import json
from decimal import Decimal, ROUND_HALF_UP

import db
from config import COMPANY_NAME

ANTHROPIC_TOOL = {
    "name": "submit_resume_score",
    "description": "Submit the scoring result for how well a resume fits a job description.",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {
                "type": "number",
                "description": "Fit score from 0 to 10 (can be a decimal, e.g. 7.5).",
            },
            "fit_summary": {
                "type": "string",
                "description": "2-4 sentence summary of overall fit against the JD.",
            },
            "positives": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Concrete strengths of this candidate relative to the JD.",
            },
            "negatives": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Concrete gaps or concerns relative to the JD. Empty list if none.",
            },
            "breakdown": {
                "type": "array",
                "description": (
                    "The 5 to 7 parameters this resume was scored on, chosen because they matter for THIS job "
                    "description (e.g. Core skills match, Relevant experience/years, Domain/industry fit, Tools and "
                    "tech stack, Education/certifications, Communication/soft skills, Location/availability). "
                    "Weights are percentages and must add up to 100."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Short parameter name, e.g. 'Tools and tech stack'."},
                        "weight": {"type": "number", "description": "Importance of this parameter for this JD, in percent."},
                        "score": {"type": "number", "description": "How well the resume meets this parameter, 0 to 10."},
                        "evidence": {
                            "type": "string",
                            "description": "One line quoted or derived from the resume that justifies the score "
                                           "(say 'No evidence in resume' if there is none).",
                        },
                    },
                    "required": ["name", "weight", "score", "evidence"],
                },
            },
        },
        "required": ["score", "fit_summary", "positives", "negatives", "breakdown"],
    },
}

OPENAI_STYLE_TOOL = {
    "type": "function",
    "function": {
        "name": ANTHROPIC_TOOL["name"],
        "description": ANTHROPIC_TOOL["description"],
        "parameters": ANTHROPIC_TOOL["input_schema"],
    },
}

PROMPT_TEMPLATE = """You are an experienced technical recruiter. Compare the RESUME below against the JOB DESCRIPTION and score how well this candidate fits the role.

Score from 0 to 10, where:
- 8-10: Excellent fit, meets or exceeds most JD requirements
- 5-7.9: Partial fit, worth a closer look, notable gaps or unclear experience
- 0-4.9: Poor fit, missing core requirements

Be specific and evidence-based in your positives/negatives - cite what's actually in the resume, don't guess.

Also break the score down: pick the 5 to 7 parameters that matter most for THIS job description (for example core skills match, relevant experience/years, domain/industry fit, tools and tech stack, education/certifications, communication/soft skills, location/availability - only those that genuinely matter for this role). Give each a weight in percent (all weights add up to 100), a 0-10 score, and one line of evidence quoted or derived from the resume. Your overall score should be the weighted average of the parameter scores.

JOB DESCRIPTION:
{jd_text}

RESUME:
{resume_text}

Call submit_resume_score with your assessment."""


class MissingApiKeyError(RuntimeError):
    pass


def _score_via_anthropic(prompt, settings):
    api_key = settings["llm_api_key"]
    if not api_key:
        raise MissingApiKeyError("No Anthropic API key set. Add one in Settings before running Score.")
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=settings["llm_model"] or "claude-sonnet-5",
        max_tokens=2048,
        tools=[ANTHROPIC_TOOL],
        tool_choice={"type": "tool", "name": "submit_resume_score"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_resume_score":
            return block.input
    raise RuntimeError("Model did not return a score via tool use.")


def _score_via_openrouter(prompt, settings):
    api_key = settings["llm_api_key"]
    if not api_key:
        raise MissingApiKeyError("No OpenRouter API key set. Add one in Settings before running Score.")
    import httpx

    response = httpx.post(
        f"{settings['llm_base_url']}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings["llm_model"] or "openai/gpt-5.4",
            "messages": [{"role": "user", "content": prompt}],
            "tools": [OPENAI_STYLE_TOOL],
            "tool_choice": {"type": "function", "function": {"name": "submit_resume_score"}},
        },
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    tool_calls = data["choices"][0]["message"].get("tool_calls") or []
    if not tool_calls:
        raise RuntimeError("Model did not return a score via tool use.")
    return json.loads(tool_calls[0]["function"]["arguments"])


MIN_BREAKDOWN_PARAMS = 3
MAX_BREAKDOWN_PARAMS = 8


def _clamp(value, low, high):
    return max(low, min(high, value))


def _round_half_up_1dp(value):
    return float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _as_number(value):
    """float for a number (or numeric string); None for anything else (incl. NaN/inf, bools)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _weights_to_100(weights):
    """Whole-number percentages, in the same order, that add up to exactly 100 and
    keep the proportions of `weights` (largest-remainder rounding). Falls back to an
    even split when the weights are all zero/negative."""
    weights = [max(w, 0.0) for w in weights]
    total = sum(weights)
    if total <= 0:
        weights = [1.0] * len(weights)
        total = float(len(weights))
    exact = [w * 100 / total for w in weights]
    floors = [int(x) for x in exact]
    remaining = 100 - sum(floors)
    order = sorted(range(len(exact)), key=lambda i: exact[i] - floors[i], reverse=True)
    for i in order[:remaining]:
        floors[i] += 1
    return floors


def normalise_breakdown(raw):
    """Validates the model's per-parameter breakdown and returns a clean list of
    {name, weight, score, evidence} - or None when it is missing/unusable (the caller
    then falls back to the model's own overall score and stores no breakdown).

    - rows without a name or without a numeric score are dropped; duplicate names too
    - scores are clamped to 0-10 (one decimal); evidence is trimmed to one short line
    - weights are rescaled to whole percentages that add up to exactly 100 (an even
      split if the model gave no usable weights); a parameter that would round to 0%
      is dropped and the rest rescaled
    - fewer than MIN_BREAKDOWN_PARAMS usable rows means 'invalid'; more than
      MAX_BREAKDOWN_PARAMS keeps the first MAX_BREAKDOWN_PARAMS"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, list):
        return None
    rows, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name") or "").split())[:60]
        score = _as_number(item.get("score"))
        if not name or score is None or name.lower() in seen:
            continue
        seen.add(name.lower())
        weight = _as_number(item.get("weight"))
        evidence = " ".join(str(item.get("evidence") or "").split())[:240]
        rows.append({
            "name": name, "weight": weight if weight is not None else 0.0,
            "score": _round_half_up_1dp(_clamp(score, 0.0, 10.0)), "evidence": evidence,
        })
    rows = rows[:MAX_BREAKDOWN_PARAMS]
    if len(rows) < MIN_BREAKDOWN_PARAMS:
        return None
    percents = _weights_to_100([r["weight"] for r in rows])
    kept = [dict(r, weight=p) for r, p in zip(rows, percents) if p > 0]
    if len(kept) < len(rows):  # drop the 0% rows, then re-normalise what is left
        if len(kept) < MIN_BREAKDOWN_PARAMS:
            return None
        percents = _weights_to_100([r["weight"] for r in kept])
        kept = [dict(r, weight=p) for r, p in zip(kept, percents)]
    return kept


def weighted_score(breakdown):
    """The overall 0-10 score: the weighted average of the parameter scores (weights are
    percentages adding up to 100), rounded to one decimal - so the popup always adds up."""
    total = sum(r["weight"] * r["score"] for r in breakdown) / sum(r["weight"] for r in breakdown)
    return _round_half_up_1dp(total)


def score_candidate(jd_text, resume_text):
    settings = db.get_llm_settings()
    prompt = PROMPT_TEMPLATE.format(jd_text=jd_text[:12000], resume_text=resume_text[:12000])

    if settings["llm_provider"] == "anthropic":
        data = _score_via_anthropic(prompt, settings)
    else:
        data = _score_via_openrouter(prompt, settings)

    breakdown = normalise_breakdown(data.get("breakdown"))
    if breakdown:
        score = weighted_score(breakdown)
    else:  # no usable breakdown: keep the model's own overall score (bucket logic is unchanged)
        score = _round_half_up_1dp(_clamp(float(data["score"]), 0.0, 10.0))

    return {
        "score": score,
        "fit_summary": data["fit_summary"],
        "positives": data.get("positives", []),
        "negatives": data.get("negatives", []),
        "breakdown": breakdown,
    }


def _score_candidates(position, candidates, progress=None):
    total = len(candidates)
    if progress is not None:
        progress["total"] = total
        progress["done"] = 0
        progress["error"] = None

    for candidate in candidates:
        try:
            result = score_candidate(position["jd_text"], candidate["resume_text"] or "")
            db.save_score(
                candidate["id"],
                result["score"],
                result["fit_summary"],
                result["positives"],
                result["negatives"],
                breakdown=result.get("breakdown"),
            )
        except Exception as exc:  # noqa: BLE001 - surface any failure to the progress state
            if progress is not None:
                progress["error"] = str(exc)
            raise
        finally:
            if progress is not None:
                progress["done"] += 1


def score_all_for_position(position_id, progress=None):
    position = db.get_position(position_id)
    candidates = db.unscored_candidates(position_id)
    _score_candidates(position, candidates, progress)


def score_selected_candidates(position_id, candidate_ids, progress=None):
    position = db.get_position(position_id)
    candidates = db.get_candidates_by_ids(candidate_ids)
    _score_candidates(position, candidates, progress)


# ---- Shared low-level dispatch for the newer, non-scoring LLM features below -
# same provider dispatch and settings lookup as score_candidate above (Anthropic
# tool-use vs OpenRouter/OpenAI-style tool-use), factored out once so each
# feature function doesn't repeat the whole client/httpx boilerplate. Every
# public function in this module still goes through db.get_llm_settings() and
# branches on settings["llm_provider"], exactly like score_candidate does.

def _anthropic_text(prompt, settings, max_tokens=1500):
    api_key = settings["llm_api_key"]
    if not api_key:
        raise MissingApiKeyError("No Anthropic API key set. Add one in Settings before using AI features.")
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=settings["llm_model"] or "claude-sonnet-5",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text").strip()


def _openrouter_text(prompt, settings, max_tokens=1500):
    api_key = settings["llm_api_key"]
    if not api_key:
        raise MissingApiKeyError("No OpenRouter API key set. Add one in Settings before using AI features.")
    import httpx

    response = httpx.post(
        f"{settings['llm_base_url']}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings["llm_model"] or "openai/gpt-5.4",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        },
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    return (data["choices"][0]["message"]["content"] or "").strip()


def _anthropic_tool_call(prompt, tool, settings, max_tokens=1500):
    api_key = settings["llm_api_key"]
    if not api_key:
        raise MissingApiKeyError("No Anthropic API key set. Add one in Settings before using AI features.")
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=settings["llm_model"] or "claude-sonnet-5",
        max_tokens=max_tokens,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == tool["name"]:
            return block.input
    raise RuntimeError("Model did not return structured output via tool use.")


def _openrouter_tool_call(prompt, tool, settings, max_tokens=1500):
    api_key = settings["llm_api_key"]
    if not api_key:
        raise MissingApiKeyError("No OpenRouter API key set. Add one in Settings before using AI features.")
    import httpx

    openai_tool = {
        "type": "function",
        "function": {"name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"]},
    }
    response = httpx.post(
        f"{settings['llm_base_url']}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings["llm_model"] or "openai/gpt-5.4",
            "messages": [{"role": "user", "content": prompt}],
            "tools": [openai_tool],
            "tool_choice": {"type": "function", "function": {"name": tool["name"]}},
        },
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    tool_calls = data["choices"][0]["message"].get("tool_calls") or []
    if not tool_calls:
        raise RuntimeError("Model did not return structured output via tool use.")
    return json.loads(tool_calls[0]["function"]["arguments"])


def _run_text(prompt, max_tokens=1500):
    settings = db.get_llm_settings()
    if settings["llm_provider"] == "anthropic":
        return _anthropic_text(prompt, settings, max_tokens)
    return _openrouter_text(prompt, settings, max_tokens)


def _run_tool(prompt, tool, max_tokens=1500):
    settings = db.get_llm_settings()
    if settings["llm_provider"] == "anthropic":
        return _anthropic_tool_call(prompt, tool, settings, max_tokens)
    return _openrouter_tool_call(prompt, tool, settings, max_tokens)


# ---- 1. AI-drafted job descriptions ------------------------------------------

def draft_job_description(title, bullets):
    """Drafts a full JD from a role title + a few bullet points. Returns plain
    text the caller pre-fills into an editable field - never auto-submitted,
    the recruiter always reviews/edits before the position is actually created."""
    bullets = (bullets or "").strip()
    prompt = (
        "You are an experienced technical recruiter writing a job description for an internal ATS.\n\n"
        f"Role title: {title}\n\n"
        + (f"Notes/requirements from the recruiter (may be rough bullet points):\n{bullets}\n\n" if bullets else "")
        + "Write a complete, well-organized job description for this role. Include sections such as "
        "an overview, key responsibilities, required skills/qualifications, and nice-to-haves. Plain text only "
        "(no markdown headers or asterisks) with clear line breaks between sections, ready to paste into a "
        f"document. Do not include placeholder brackets like [Company Name] - use '{COMPANY_NAME}' as the company."
    )
    return _run_text(prompt, max_tokens=1500)


# ---- 2. AI-drafted emails (a short personal note, not the whole template) ---

def draft_email_note(candidate_name, role_name, context_label):
    """Drafts a short (2-4 sentence) personal note for a recruiter to review/edit
    and optionally attach to an outgoing candidate email. Does not touch subject/
    template wording - those stay the fixed email templates; this is the
    one part of the email meant to be personalized per send."""
    prompt = (
        f"You are a friendly, professional recruiter at {COMPANY_NAME} writing a short personal note to add to "
        f"an outgoing email. Candidate name: {candidate_name}. Role: {role_name}. Email context: {context_label}.\n\n"
        "Write 2-4 warm, specific sentences (no greeting/signoff - those are handled separately by the email "
        "template this gets inserted into). Plain text only, no markdown."
    )
    return _run_text(prompt, max_tokens=300)


# ---- 3. Screening assessments -------------------------------------------------

ASSESSMENT_QUESTIONS_TOOL = {
    "name": "submit_assessment_questions",
    "description": "Submit a list of skill-testing screening questions derived from a job description.",
    "input_schema": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The screening questions, most important first.",
            },
        },
        "required": ["questions"],
    },
}

ASSESSMENT_SCORE_TOOL = {
    "name": "submit_assessment_score",
    "description": "Submit a score and assessment for a candidate's written screening answers.",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {"type": "number", "description": "Score from 0 to 10 (can be a decimal, e.g. 7.5)."},
            "summary": {"type": "string", "description": "2-4 sentence summary of how well the answers show fit for the role."},
            "positives": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Concrete strengths shown in the answers.",
            },
            "negatives": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Concrete gaps or weak/incomplete answers. Empty list if none.",
            },
        },
        "required": ["score", "summary", "positives", "negatives"],
    },
}


def generate_assessment_questions(jd_text, n=5):
    """Drafts N skill-testing screening questions from a JD. Returned list is
    shown to the recruiter to edit before Save persists it as this position's
    real question set - never saved directly from this call."""
    prompt = (
        f"You are an experienced technical recruiter. Based on the JOB DESCRIPTION below, write {n} skill-testing "
        "screening questions that would help assess whether a candidate is a good fit. Mix practical/scenario "
        "questions with a couple of knowledge-check questions. Keep each question self-contained (a candidate "
        "should be able to answer in a few sentences of free text, no code execution required).\n\n"
        f"JOB DESCRIPTION:\n{jd_text[:12000]}\n\n"
        "Call submit_assessment_questions with your list."
    )
    data = _run_tool(prompt, ASSESSMENT_QUESTIONS_TOOL, max_tokens=1200)
    return [q.strip() for q in data.get("questions", []) if q and q.strip()][:n]


def score_assessment_answers(jd_text, qa_pairs):
    """qa_pairs: list of (question_text, answer_text). Returns the same shape as
    score_candidate: {score, summary, positives, negatives}."""
    qa_block = "\n\n".join(f"Q: {q}\nA: {a or '(no answer given)'}" for q, a in qa_pairs)
    prompt = (
        "You are an experienced technical recruiter reviewing a candidate's written answers to a screening "
        "assessment for the role described below. Score how well the answers demonstrate fit for the role.\n\n"
        "Score from 0 to 10, where 8-10 is excellent, 5-7.9 is partial/worth a closer look, 0-4.9 is poor. "
        "Be specific and evidence-based - cite what's actually in the answers, don't guess.\n\n"
        f"JOB DESCRIPTION:\n{jd_text[:8000]}\n\n"
        f"SCREENING QUESTIONS AND ANSWERS:\n{qa_block[:8000]}\n\n"
        "Call submit_assessment_score with your assessment."
    )
    data = _run_tool(prompt, ASSESSMENT_SCORE_TOOL, max_tokens=1200)
    return {
        "score": float(data["score"]),
        "summary": data["summary"],
        "positives": data.get("positives", []),
        "negatives": data.get("negatives", []),
    }


# ---- 4. Sourcing/screening chatbot --------------------------------------------

def chatbot_answer(jd_text, candidates_context, history, question):
    """Answers a recruiter's question about a position, grounded only in that
    position's real JD text and candidate list/scores already in the DB -
    no external lookups (LinkedIn etc). history: list of {role, content} dicts
    from the current page session (not persisted server-side)."""
    history_block = ""
    for turn in (history or [])[-8:]:
        role = "Recruiter" if turn.get("role") == "user" else "Assistant"
        history_block += f"{role}: {turn.get('content', '')}\n"

    prompt = (
        "You are an internal recruiting assistant. Answer the recruiter's question using ONLY the job "
        "description and candidate information given below - this is the app's own data, not a general "
        "knowledge question. If something isn't covered by the data given, say so plainly rather than "
        "guessing or inventing details (e.g. don't claim to look anyone up on LinkedIn or elsewhere).\n\n"
        f"JOB DESCRIPTION:\n{jd_text[:8000]}\n\n"
        f"CANDIDATES (name, fit score /10, bucket, short summary):\n{candidates_context[:8000]}\n\n"
        + (f"CONVERSATION SO FAR:\n{history_block}\n" if history_block else "")
        + f"Recruiter's question: {question}\n\n"
        "Answer concisely and specifically, referencing actual candidate names/scores where relevant."
    )
    return _run_text(prompt, max_tokens=800)


# ---- 5. LLM-assisted interview scorecards -------------------------------------

SCORECARD_TOOL = {
    "name": "submit_scorecard",
    "description": "Submit a structured interview scorecard summary built from a candidate's interview notes.",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {"type": "number", "description": "Overall score from 0 to 10 based on what the notes describe."},
            "summary": {"type": "string", "description": "2-4 sentence structured summary of how the interview(s) went."},
            "positives": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Concrete strengths/positive signals mentioned in the notes.",
            },
            "negatives": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Concrete concerns/gaps mentioned in the notes. Empty list if none.",
            },
        },
        "required": ["score", "summary", "positives", "negatives"],
    },
}


# ---- 6. Interview & assessment question recommendations (NOT a scored assessment) -
# Deliberately distinct from generate_assessment_questions above: that feature
# produces a formal question set the candidate answers and gets scored on.
# This one is just a pick-and-choose reference list for the interviewer - two
# labeled buckets (things to ask out loud in the interview vs. skill/assessment
# checks worth including) - never saved as "the" assessment, never sent
# anywhere, never scored.

INTERVIEW_QUESTION_RECOMMENDATIONS_TOOL = {
    "name": "submit_interview_question_recommendations",
    "description": "Submit suggested interview and assessment questions for a recruiter/interviewer to choose from.",
    "input_schema": {
        "type": "object",
        "properties": {
            "interview_questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Questions to ask out loud in an interview (behavioral, situational, role-fit).",
            },
            "assessment_questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Skill/knowledge-check questions worth including in a written or take-home assessment.",
            },
        },
        "required": ["interview_questions", "assessment_questions"],
    },
}


def generate_interview_question_recommendations(jd_text, resume_text=None, candidate_name=None):
    """Returns {"interview_questions": [...], "assessment_questions": [...]} -
    a plain recommendations list an interviewer can look at and pick from, not
    a formal scored assessment object and not something auto-sent to anyone.
    resume_text/candidate_name are optional - when given, questions are tuned
    to that specific candidate's background against the JD; when omitted,
    the questions are JD-only (position-level, useful before any resumes are
    even in yet)."""
    candidate_block = ""
    if resume_text and resume_text.strip():
        who = candidate_name or "this candidate"
        candidate_block = (
            f"\n\nCANDIDATE RESUME ({who}), so you can tailor some questions to their specific background "
            f"and probe any gaps/claims worth verifying:\n{resume_text[:8000]}\n"
        )
    prompt = (
        "You are an experienced technical recruiter/hiring manager preparing for an interview. Based on the "
        "JOB DESCRIPTION below" + (" and the candidate's resume" if candidate_block else "") + ", suggest a useful "
        "pool of questions for the interviewer to look at and choose from - NOT a fixed script, just good "
        "options. Give a mix: behavioral/situational interview questions, and separately, skill/knowledge-check "
        "questions suitable for a written or take-home assessment. Aim for roughly 6-10 of each.\n\n"
        f"JOB DESCRIPTION:\n{jd_text[:10000]}\n"
        f"{candidate_block}\n"
        "Call submit_interview_question_recommendations with both lists."
    )
    data = _run_tool(prompt, INTERVIEW_QUESTION_RECOMMENDATIONS_TOOL, max_tokens=1500)
    return {
        "interview_questions": [q.strip() for q in data.get("interview_questions", []) if q and q.strip()],
        "assessment_questions": [q.strip() for q in data.get("assessment_questions", []) if q and q.strip()],
    }


def generate_scorecard_from_notes(jd_text, candidate_name, notes_text):
    """Turns a candidate's existing Quick Capture notes into a structured
    interview scorecard summary - same {score, summary, positives, negatives}
    shape as score_candidate, generated and saved immediately (same convention
    as the existing resume-scoring feature, not a draft-then-confirm one)."""
    prompt = (
        f"You are an experienced technical recruiter. Below are raw interview/recruiting notes taken about "
        f"{candidate_name}, a candidate for the role described below. Turn these notes into a structured "
        "interview scorecard: an overall score, a short summary, and concrete positives/concerns. Base this "
        "only on what the notes actually say - if the notes are thin, say so in the summary rather than "
        "inventing detail.\n\n"
        f"JOB DESCRIPTION:\n{jd_text[:6000]}\n\n"
        f"NOTES (newest first, each with when it was logged):\n{notes_text[:6000]}\n\n"
        "Call submit_scorecard with your assessment."
    )
    data = _run_tool(prompt, SCORECARD_TOOL, max_tokens=1000)
    return {
        "score": float(data["score"]),
        "summary": data["summary"],
        "positives": data.get("positives", []),
        "negatives": data.get("negatives", []),
    }
