"""Quick Capture parsing: turn one free-typed sentence into a candidate match
+ an optional date/time + an optional stage-flag update, entirely with plain
code - no LLM. Each piece is a separate, well-understood problem:
  - parsedatetime: finds a date/time phrase anywhere in the sentence.
  - rapidfuzz: fuzzy-matches whatever's left against the known candidate list,
    since HR Hiring Agent already owns that list - this is a lookup, not
    open-ended name detection, so it doesn't need NER/an LLM either.
  - a small keyword/regex ruleset: maps phrasing like "selected for L2" or
    "rejected" onto the app's own real pipeline flags (l2_scheduled, etc.),
    reusing the exact field names/labels the rest of the app already shows.
"""

import re
from datetime import datetime

import parsedatetime
from rapidfuzz import fuzz, process, utils

import db

_calendar = parsedatetime.Calendar()

MATCH_THRESHOLD = 60

# parsedatetime has no fuzzy matching of its own - a misspelled date/time word
# (e.g. "tommorow") silently fails to parse at all rather than degrading
# gracefully, which is a real gap since the rest of Quick Capture is
# deliberately typo-tolerant. This is a closed vocabulary of the date/time
# words parsedatetime actually recognizes, used to correct near-miss
# spellings before handing text to it - not general spelling correction, so
# it can't misfire on unrelated words. Ported verbatim from the identical fix
# in Sales Pulse's capture.py (found and fixed there first, same day).
_DATE_VOCAB = [
    "today", "tomorrow", "yesterday", "tonight", "morning", "afternoon",
    "evening", "noon", "midnight", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday", "january", "february",
    "march", "april", "june", "july", "august", "september",
    "october", "november", "december", "next", "week", "weeks",
    "month", "months", "hour", "hours", "minute", "minutes",
]
_DATE_TYPO_MATCH_THRESHOLD = 82


def _typo_correct_date_words(text):
    """Best-effort correction of misspelled date/time words against the
    closed _DATE_VOCAB list, returning (corrected_text, corrections) where
    corrections is an ordered list of (orig_start, orig_end, corrected_start,
    corrected_end) for mapping a span found in corrected_text back to its
    real position in the original raw text."""
    edits = []
    for tok in re.finditer(r"[A-Za-z]+", text):
        word = tok.group(0)
        low = word.lower()
        if low in _DATE_VOCAB or len(word) < 4:
            continue
        match = process.extractOne(low, _DATE_VOCAB, scorer=fuzz.ratio)
        if match and match[0] != low and match[1] >= _DATE_TYPO_MATCH_THRESHOLD:
            edits.append((tok.start(), tok.end(), match[0]))
    if not edits:
        return text, []
    out = []
    pos = 0
    delta = 0
    corrections = []
    for orig_start, orig_end, canonical in edits:
        out.append(text[pos:orig_start])
        corrected_start = orig_start + delta
        out.append(canonical)
        corrected_end = corrected_start + len(canonical)
        corrections.append((orig_start, orig_end, corrected_start, corrected_end))
        delta += len(canonical) - (orig_end - orig_start)
        pos = orig_end
    out.append(text[pos:])
    return "".join(out), corrections


def _map_span_to_original(start_c, end_c, corrections):
    """Shift a (start, end) span found in corrected_text back to raw_text
    coordinates. Maps the start and end points independently so a span
    covering multiple words (some corrected, some not - e.g. "wednessday
    afternon") maps to the full original phrase rather than truncating at
    the first corrected token."""

    def map_point(pos, snap_to_end):
        delta = 0
        for orig_start, orig_end, corrected_start, corrected_end in corrections:
            if corrected_start < pos < corrected_end:
                return orig_end if snap_to_end else orig_start
            if corrected_start >= pos:
                break
            delta += (orig_end - orig_start) - (corrected_end - corrected_start)
        return pos + delta

    return map_point(start_c, snap_to_end=False), map_point(end_c, snap_to_end=True)

_STAGE_LABELS = {f: label for f, label, _group in db.STAGE_FIELDS}
_STAGE_LABELS.update({f: label for f, label in db.STATUS_FIELDS})

_ROUND_RE = re.compile(r"\bl(?:evel)?\s*([123])\b|\bround\s*([123])\b|\bfinal\s*round\b", re.I)

# Checked in this exact order - candidate-level outcomes first (unambiguous,
# no round needed), then round-scoped outcomes from most to least specific,
# so "not selected" can never fall through and get mis-read as "selected".
_STATUS_RULES = [
    ("joined", re.compile(r"\bjoin(?:ed|ing)?\b", re.I)),
    ("offer_made", re.compile(r"\boffer(?:ed)?\b", re.I)),
    ("on_hold", re.compile(r"\bon\s*hold\b|\bhold(?:ing)?\b", re.I)),
    ("rejected", re.compile(r"\brejected\b|\bnot\s+selected\b|\bdeclined\b|\bnot\s+cleared\b|\bdidn'?t\s+clear\b|\bfailed\b", re.I)),
]

# (suffix, regex) - suffix is appended to "l{round}_" once a round is known.
# "selected for"/"scheduled for" means queued for that round, not that it's
# already been cleared - that distinction is exactly what caused real
# confusion earlier this session when reading "selected for L2" by hand.
_ROUND_OUTCOME_RULES = [
    ("cleared", re.compile(r"\b(cleared|passed|selected\s+in|selected\s+after)\b", re.I)),
    ("scheduled", re.compile(r"\b(selected\s+for|scheduled?\s+for|schedule|invited\s+for|book(?:ed)?)\b", re.I)),
    ("attended", re.compile(r"\b(attended|interviewed)\b", re.I)),
]


def detect_round(text):
    m = _ROUND_RE.search(text)
    if not m:
        return None
    if "final" in m.group(0).lower():
        return 3
    return int(m.group(1) or m.group(2))


def detect_stage_signal(text):
    """Best-effort: one (field, label, span) to propose as a flag update, or
    None if nothing confident was found - never guessed silently, always
    shown to the user to confirm before it's actually applied."""
    for field, pattern in _STATUS_RULES:
        m = pattern.search(text)
        if m:
            return {"field": field, "label": _STAGE_LABELS[field], "span": {"start": m.start(), "end": m.end()}}

    round_num = detect_round(text)
    if round_num is None:
        return None
    for suffix, pattern in _ROUND_OUTCOME_RULES:
        m = pattern.search(text)
        if m:
            field = f"l{round_num}_{suffix}"
            return {
                "field": field,
                "label": f"L{round_num} Interview - {_STAGE_LABELS[field]}",
                "span": {"start": m.start(), "end": m.end()},
            }
    return None


_ROUND_OUTCOME_FIELD_RE = re.compile(r"^l([123])_(?:scheduled|attended|cleared)$")


def stage_field_label(field):
    """Human label for an already-applied stage field, e.g. 'l2_scheduled'
    -> 'L2 Interview - Scheduled', 'rejected' -> 'Rejected' - same format
    detect_stage_signal uses, exposed here so callers that only have the
    field name after the fact (e.g. app.py's capture_save, building the
    Input Logs action summary) don't duplicate the formatting logic."""
    m = _ROUND_OUTCOME_FIELD_RE.match(field)
    if m:
        return f"L{m.group(1)} Interview - {_STAGE_LABELS[field]}"
    return _STAGE_LABELS.get(field, field)


def parse_date(text, now=None):
    now = now or datetime.now()
    corrected, _ = _typo_correct_date_words(text)
    dt, flag = _calendar.parseDT(corrected, sourceTime=now)
    return dt if flag != 0 else None


def find_date_span(text, now=None):
    """Where the date/time phrase actually sits in the raw text (character
    offsets), for live highlighting - parseDT() alone only gives the resolved
    datetime, not its location, but Calendar.nlp() returns both. Parses
    against a typo-corrected copy (see _typo_correct_date_words) but always
    returns offsets/text from the original raw string, mapped back via
    _map_span_to_original when a correction was applied."""
    now = now or datetime.now()
    corrected, corrections = _typo_correct_date_words(text)
    results = _calendar.nlp(corrected, sourceTime=now)
    if not results:
        return None
    _dt, _flag, start, end, matched_text = results[0]
    if corrections:
        start, end = _map_span_to_original(start, end, corrections)
        return {"start": start, "end": end, "text": text[start:end]}
    return {"start": start, "end": end, "text": matched_text}


def find_name_span(text, candidate_name):
    """Best-effort location of the matched candidate's name within the raw
    text, for highlighting - the fuzzy match itself doesn't give a position,
    so this separately looks for whichever of the name's words literally
    appear (case-insensitive) and highlights the span covering them."""
    if not candidate_name:
        return None
    lower_text = text.lower()
    spans = []
    for word in candidate_name.split():
        idx = lower_text.find(word.lower())
        if idx != -1:
            spans.append((idx, idx + len(word)))
    if not spans:
        return None
    return {"start": min(s for s, _ in spans), "end": max(e for _, e in spans)}


# Bug found and fixed (audited against a realistic candidate list): plain
# token_set_ratio against the *whole* typed sentence has two real failure
# modes once the sentence carries more than just the name:
#   1. A short, single-word candidate name (e.g. "Liam") gets diluted by
#      the rest of a realistic sentence - "Lieam selected for L2, schedule
#      tommorow" (one-letter typo) scored the real candidate only 25-35%,
#      not even appearing in the top-3 suggestions.
#   2. Worse, a common surname shared by multiple real candidates
#      (a realistic candidate list can easily hold three "*Yadav"s) let
#      token_set_ratio's subset-containment rule silently pick the WRONG one:
#      "kabeer yadav cleared L1 next monday" auto-matched "Rohan Yadav" at 67%
#      confidence (above MATCH_THRESHOLD) instead of the intended "Kabir Yadav" -
#      exactly the "silently filed against the wrong person" failure this
#      app's design explicitly guards against elsewhere.
# Fix: also score the candidate's name against every contiguous same-length
# word window of the typed text (_name_window_score), so a name's score
# reflects how well it matches the words that are actually name-shaped,
# not the whole sentence around it. That alone is not safe to trust
# directly, though - two random English words of similar length can
# coincidentally align well by plain edit-distance (verified: "random
# gibberish text zzz xtreme" scored "Arvind" 61.5% via windowing alone,
# which would have cleared the old MATCH_THRESHOLD=60 as a false auto-match).
# WINDOW_PROMOTE_THRESHOLD=75 was picked with a wide, tested margin above
# every garbage-text windowed score observed (<=61.5) and below every real
# typo'd/combined-sentence match observed (>=85.7) against a realistic
# candidate list - so a windowed score only counts when it's confidently a
# name, never as a tiebreaker nudge. The final score is
# max(whole-sentence token_set_ratio, gated window score), so nothing that
# already matched via the original whole-text approach regresses.
WINDOW_PROMOTE_THRESHOLD = 75
_WORD_RE = re.compile(r"[A-Za-z0-9']+")


def _name_window_score(text, name):
    """Best plain-ratio score comparing `name` against any contiguous
    word-window of `text` the same length as `name` - confines the
    comparison to a name-shaped slice of the sentence instead of the whole
    thing, which is what actually rescues a typo'd name once it's buried in
    a realistic capture sentence (see the module-level comment above)."""
    words = _WORD_RE.findall(text)
    if not words:
        return 0
    k = len(name.split())
    best = 0
    for i in range(max(1, len(words) - k + 1)):
        window = " ".join(words[i:i + k])
        if not window:
            continue
        score = fuzz.ratio(window, name, processor=utils.default_process)
        if score > best:
            best = score
    return best


def top_candidate_matches(text, candidates, limit=3):
    if not candidates:
        return []
    scored = []
    for c in candidates:
        name = c["candidate_name"]
        whole_text_score = fuzz.token_set_ratio(text, name, processor=utils.default_process)
        window_score = _name_window_score(text, name)
        promoted_window_score = window_score if window_score >= WINDOW_PROMOTE_THRESHOLD else 0
        scored.append((c, max(whole_text_score, promoted_window_score)))
    scored.sort(key=lambda pair: -pair[1])
    return [{**c, "score": round(score)} for c, score in scored[:limit]]


def parse_capture(text, candidates, now=None):
    text = text.strip()
    matches = top_candidate_matches(text, candidates, limit=3)
    best = matches[0] if matches and matches[0]["score"] >= MATCH_THRESHOLD else None
    parsed_dt = parse_date(text, now=now)
    date_span = find_date_span(text, now=now)
    return {
        "raw_text": text,
        "matched_candidate": best,
        "parsed_date": parsed_dt.isoformat() if parsed_dt else None,
        "parsed_date_display": parsed_dt.strftime("%b %d, %Y %I:%M %p") if parsed_dt else None,
        "suggestions": [] if best else matches,
        "date_span": date_span,
        "name_span": find_name_span(text, best["candidate_name"]) if best else None,
        "stage_signal": detect_stage_signal(text),
    }
