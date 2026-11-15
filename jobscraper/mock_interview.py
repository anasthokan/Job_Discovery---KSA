"""Run a mock interview with Ollama from a role and facts the caller supplied.

The client keeps the session object and sends it back with each answer.
Nothing is stored on the server. Employers and numbers that were not in the
facts or the answer are left out of the suggested answer.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

from jobscraper.jd_to_cv import parse_job_description
from jobscraper.ollama_resume import OllamaError, active_model, complete

_CATEGORIES = {"introduction", "experience", "technical", "behavioral", "closing", "general"}
_FOCUSES = {"mixed", "behavioral", "technical"}
_ORDERS = {
    "mixed": [
        "introduction",
        "experience",
        "technical",
        "behavioral",
        "closing",
        "technical",
        "behavioral",
        "experience",
    ],
    "behavioral": [
        "introduction",
        "behavioral",
        "experience",
        "behavioral",
        "closing",
        "behavioral",
        "experience",
        "behavioral",
    ],
    "technical": [
        "introduction",
        "technical",
        "experience",
        "technical",
        "closing",
        "technical",
        "experience",
        "technical",
    ],
}

_QUESTION_RULES = """You are preparing a mock job interview.
Return one JSON object and nothing else. No markdown.

Rules:
- questions: exactly {count} objects.
- Each object has category and prompt.
- category is one of: introduction, experience, technical, behavioral, closing.
- prompt is one question, one or two sentences.
- Use the candidate's real employers, titles, and skills from FACTS when they are present.
- Do not invent employers, projects, degrees, or metrics.
- Do not ask about salary, visa status, age, or health.
- The first question is an introduction. The last question is a closing question.
- focus is {focus}. mixed uses several categories. behavioral favors workplace examples. technical favors skills and how the work was done.
Keys: questions.
"""

_EVAL_RULES = """You are scoring one mock-interview answer.
Return one JSON object and nothing else. No markdown.

Rules:
- score: integer from 1 to 10. 1 is off-topic or has no substance. 10 is specific, relevant, and complete.
- summary: one or two sentences about this answer only.
- strengths: 1 to 3 short strings about what the answer already did well. Use an empty list when nothing was specific.
- improvements: 1 to 3 short strings about what to add next time.
- better_answer: a stronger version of this answer in the candidate's voice. Use only employers, skills, and numbers that appear in FACTS or ANSWER. If the answer has no specific fact, describe the situation-action-result shape without inventing a story.
- Do not say the candidate already works at the hiring company unless that company is in FACTS.
Keys: score, summary, strengths, improvements, better_answer.
"""

_NUMBER_RE = re.compile(r"(?:\$\s*)?\d[\d,]*(?:\.\d+)?%?")
_EMPLOYED_RE = re.compile(
    r"(?i)\b(?:i am|i'm|i|you are|you're|you)\s+(?:currently\s+|already\s+)?"
    r"(?:work|working|worked|employed)\w*\s+(?:at|for|with)\s+{name}\b"
)

Writer = Callable[..., str]


class MockInterviewError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def start_interview(
    facts: dict[str, Any],
    *,
    job: dict[str, Any] | None = None,
    target_title: str = "",
    question_count: int = 5,
    focus: str = "mixed",
    writer: Writer | None = None,
) -> dict[str, Any]:
    """Return the first question and a session the client sends back. Raises MockInterviewError."""
    count = _question_count(question_count)
    chosen = _focus(focus)
    cv = _copy_facts(facts)
    title, company, location, description = _resolve_role(job, target_title)
    prompt = _QUESTION_RULES.format(count=count, focus=chosen) + "\n\nFACTS:\n" + _prompt_facts(
        cv, title, company, location, description
    )
    try:
        raw, model_name = _write(prompt, writer, num_predict=800)
    except OllamaError as exc:
        raise MockInterviewError(str(exc), 503) from exc

    generated = _extract_json(raw)
    questions = _questions_from_model(generated, count)
    source = "model"
    if questions is None:
        questions = _template_questions(title, company, cv, count, chosen)
        source = "template"

    session = {
        "v": 1,
        "id": uuid.uuid4().hex[:12],
        "role": title,
        "company": company,
        "location": location,
        "focus": chosen,
        "description": _clip(description, 1200),
        "facts": _session_facts(cv),
        "questions": questions,
        "turns": [],
    }
    return _view(
        session,
        model_name,
        question_source=source,
        feedback=None,
        answered=None,
        note=(
            "Send this session back with each answer. "
            "Questions use the role and the facts you sent. "
            "A suggested answer does not add employers or numbers that were not in those facts or in the answer."
        ),
    )


def submit_answer(
    session: dict[str, Any],
    answer: str,
    *,
    writer: Writer | None = None,
) -> dict[str, Any]:
    """Score one answer and return the next question, or the closing report."""
    state = _load_session(session)
    questions = state["questions"]
    turns = state["turns"]
    if len(turns) >= len(questions):
        raise MockInterviewError(
            "This interview is already complete. Start a new one to practice again.",
            400,
        )
    text = re.sub(r"\s+", " ", (answer or "").strip())
    if len(text) < 12:
        raise MockInterviewError("Write at least a sentence so the answer can be scored.", 400)
    text = text[:4000]

    question = questions[len(turns)]
    facts = state.get("facts") or {}
    prompt = _EVAL_RULES + "\n\nFACTS:\n" + json.dumps(
        {
            "role": state.get("role"),
            "company": state.get("company"),
            "job_description": state.get("description") or None,
            "candidate": facts,
            "question": question.get("prompt"),
            "answer": text[:2000],
        },
        ensure_ascii=False,
    )
    try:
        raw, model_name = _write(prompt, writer, num_predict=500)
    except OllamaError as exc:
        raise MockInterviewError(str(exc), 503) from exc

    feedback = _feedback_from_model(raw, facts, text, question.get("prompt") or "", state.get("company") or "")
    turns.append(
        {
            "question_id": question["id"],
            "category": question["category"],
            "question": question["prompt"],
            "answer": text,
            "score": feedback["score"],
            "summary": feedback["summary"],
            "strengths": feedback["strengths"],
            "improvements": feedback["improvements"],
            "better_answer": feedback["better_answer"],
            "source": feedback["source"],
        }
    )
    return _view(
        state,
        model_name,
        question_source=None,
        feedback=feedback,
        answered={
            "number": len(turns),
            "category": question["category"],
            "prompt": question["prompt"],
            "answer": text,
        },
        note="Each answer is scored on its own. The closing report averages those scores.",
    )


def _view(
    session: dict[str, Any],
    model_name: str,
    *,
    question_source: str | None,
    feedback: dict[str, Any] | None,
    answered: dict[str, Any] | None,
    note: str,
) -> dict[str, Any]:
    questions = session["questions"]
    turns = session["turns"]
    done = len(turns) >= len(questions)
    body: dict[str, Any] = {
        "ok": True,
        "mode": "interview",
        "model": model_name,
        "interview_id": session.get("id"),
        "status": "complete" if done else "in_progress",
        "focus": session.get("focus"),
        "progress": {"answered": len(turns), "total": len(questions)},
        "job": {
            "title": session.get("role") or None,
            "company": session.get("company") or None,
            "location": session.get("location") or None,
        },
        "note": note,
        "answered": answered,
        "feedback": feedback,
        "question": None if done else _question_view(questions, len(turns)),
        "report": _report(session) if done else None,
        "session": session,
    }
    if question_source:
        body["question_source"] = question_source
    return body


def _report(session: dict[str, Any]) -> dict[str, Any]:
    turns = session.get("turns") or []
    scores = [int(turn.get("score") or 0) for turn in turns]
    overall = int(round(sum(scores) / len(scores))) if scores else 0
    title = session.get("role") or "this role"
    company = session.get("company") or ""
    where = f" at {company}" if company else ""
    strengths: list[str] = []
    improvements: list[str] = []
    recap = []
    for index, turn in enumerate(turns, start=1):
        _take(strengths, turn.get("strengths") or [])
        _take(improvements, turn.get("improvements") or [])
        recap.append(
            {
                "number": index,
                "category": turn.get("category"),
                "question": turn.get("question"),
                "answer": turn.get("answer"),
                "score": turn.get("score"),
                "summary": turn.get("summary"),
            }
        )
    return {
        "overall_score": overall,
        "summary": (
            f"You completed a {len(turns)}-question {session.get('focus') or 'mixed'} interview "
            f"for the {title} role{where}. Your average score is {overall} out of 10."
        ),
        "strengths": strengths[:5],
        "improvements": improvements[:5],
        "turns": recap,
    }


def _take(bucket: list[str], values: list[Any]) -> None:
    for value in values:
        text = _one_line(str(value or ""))
        if len(text) < 8 or text in bucket:
            continue
        bucket.append(text[:240])
        return


def _question_view(questions: list[dict[str, Any]], index: int) -> dict[str, Any]:
    item = questions[index]
    return {
        "number": index + 1,
        "total": len(questions),
        "category": item.get("category"),
        "prompt": item.get("prompt"),
    }


def _load_session(session: Any) -> dict[str, Any]:
    state = session
    if isinstance(state, dict) and "questions" not in state and isinstance(state.get("session"), dict):
        state = state["session"]
    if not isinstance(state, dict) or state.get("v") != 1:
        raise MockInterviewError("Send the session from the previous mock interview response.", 400)
    questions = state.get("questions")
    turns = state.get("turns")
    if not isinstance(questions, list) or not questions or not isinstance(turns, list):
        raise MockInterviewError("Send the session from the previous mock interview response.", 400)
    if len(turns) > len(questions):
        raise MockInterviewError("Send the session from the previous mock interview response.", 400)
    return state


def _write(prompt: str, writer: Writer | None, *, num_predict: int) -> tuple[str, str]:
    if writer is not None:
        return writer(prompt), "test"
    text = complete(
        "Return only the JSON object described in the user message.",
        prompt,
        temperature=0.3,
        num_predict=num_predict,
        timeout=75,
    )
    return text, active_model() or "ollama"


def _resolve_role(job: dict[str, Any] | None, target_title: str) -> tuple[str, str, str, str]:
    posting = job or {}
    description = str(posting.get("description") or "").strip()
    company = str(posting.get("company") or "").strip()
    location = str(posting.get("location") or "").strip()
    title = (target_title or "").strip() or str(posting.get("title") or "").strip()
    if not title and len(description) >= 40:
        parsed = parse_job_description(description, company=company or None, location=location or None)
        title = str(parsed.get("title") or "").strip()
        company = company or str(parsed.get("company") or "").strip()
        location = location or str(parsed.get("location") or "").strip()
    if not title and not company and len(description) < 40:
        raise MockInterviewError(
            "Send a job title, company, job description, or job_id so the interview has a role.",
            400,
        )
    return title, company, location, description


def _question_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 3 <= value <= 8:
        raise MockInterviewError("question_count must be from 3 to 8.", 400)
    return value


def _focus(value: str) -> str:
    chosen = (value or "mixed").strip().lower()
    if chosen not in _FOCUSES:
        raise MockInterviewError("focus must be mixed, behavioral, or technical.", 400)
    return chosen


def _questions_from_model(generated: dict[str, Any] | None, count: int) -> list[dict[str, Any]] | None:
    rows = (generated or {}).get("questions") if isinstance(generated, dict) else None
    if not isinstance(rows, list) or len(rows) < count:
        return None
    accepted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            return None
        prompt = _one_line(str(item.get("prompt") or item.get("question") or ""))
        if not 24 <= len(prompt) <= 500:
            return None
        key = prompt.lower()
        if key in seen:
            return None
        seen.add(key)
        category = str(item.get("category") or "general").strip().lower()
        if category not in _CATEGORIES:
            category = "general"
        accepted.append({"id": len(accepted) + 1, "category": category, "prompt": prompt})
        if len(accepted) == count:
            first = accepted[0]["category"]
            last = accepted[-1]["category"]
            if first != "introduction" or last != "closing":
                return None
            return accepted
    return None


def _skill_phrase(skills: list[str]) -> str:
    names = [str(item).strip() for item in skills[:3] if str(item).strip()]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]}, {names[1]}, and {names[2]}"


def _template_questions(
    title: str,
    company: str,
    cv: dict[str, Any],
    count: int,
    focus: str,
) -> list[dict[str, Any]]:
    role_name = title or "this role"
    where = f" at {company}" if company else ""
    roles = [role for role in (cv.get("experience") or []) if isinstance(role, dict)]
    role = roles[0] if roles else {}
    role_title = str(role.get("title") or "").strip()
    employer = str(role.get("company") or "").strip()
    skills = list(cv.get("skills") or [])
    skill = _skill_phrase(skills)
    projects = [item for item in (cv.get("projects") or []) if isinstance(item, dict)]
    project_name = str((projects[0] if projects else {}).get("name") or "").strip()
    if employer and role_title:
        experience = f"Walk me through your work as {role_title} at {employer}. What did you personally deliver?"
    elif employer:
        experience = f"Walk me through your work at {employer}. What did you personally deliver?"
    elif project_name:
        experience = f"Walk me through the {project_name} project. What did you personally build?"
    else:
        experience = f"Which part of your background is the closest match for the {role_name} role, and why?"
    if skill:
        technical = f"Tell me about a time you used {skill} on a real piece of work. What was the result?"
    else:
        technical = f"Which skill would you rely on first in the {role_name} role, and where did you last use it?"
    banks = {
        "introduction": [
            f"Tell me about yourself and why you want the {role_name} role{where}.",
        ],
        "experience": [
            experience,
            "What is a result you are proud of in your recent work, and what was your part in it?",
        ],
        "technical": [
            technical,
            f"How do you check that your work is correct before you call a {role_name} task done?",
            "Tell me about a problem you debugged. What was the cause, and how did you find it?",
        ],
        "behavioral": [
            "Describe a time you disagreed with a teammate. What did you do, and what changed afterward?",
            "Tell me about a deadline you were at risk of missing. How did you handle it?",
            "Describe a time you had to learn something quickly to finish the work.",
        ],
        "closing": [
            f"What would you want to learn in your first 90 days in the {role_name} role{where}?",
        ],
    }
    used = {name: 0 for name in banks}
    questions = []
    for category in _ORDERS[focus][:count]:
        bank = banks[category]
        prompt = bank[used[category] % len(bank)]
        used[category] += 1
        questions.append({"id": len(questions) + 1, "category": category, "prompt": prompt})
    return questions


def _feedback_from_model(
    raw: str,
    facts: dict[str, Any],
    answer: str,
    question: str,
    hiring_company: str,
) -> dict[str, Any]:
    generated = _extract_json(raw)
    allowed_numbers = _number_keys(json.dumps(facts, ensure_ascii=False) + "\n" + answer + "\n" + question)
    known = {
        str(role.get("company") or "").strip().lower()
        for role in (facts.get("experience") or [])
        if isinstance(role, dict) and str(role.get("company") or "").strip()
    }
    parsed = _accept_feedback(generated, allowed_numbers, hiring_company, known)
    if parsed is None:
        return _rubric(answer, facts)
    return parsed


def _accept_feedback(
    generated: dict[str, Any] | None,
    allowed_numbers: set[str],
    hiring_company: str,
    known_companies: set[str],
) -> dict[str, Any] | None:
    if not isinstance(generated, dict):
        return None
    score = _score(generated.get("score"))
    summary = _one_line(str(generated.get("summary") or ""))
    if score is None or len(summary) < 20:
        return None
    strengths = _short_list(generated.get("strengths"))
    improvements = _short_list(generated.get("improvements"))
    better = _one_line(str(generated.get("better_answer") or ""))
    pieces = [summary, *strengths, *improvements]
    if any(_has_new_number(piece, allowed_numbers) for piece in pieces):
        return None
    if any(_claims_hiring_job(piece, hiring_company, known_companies) for piece in pieces):
        return None
    if not improvements:
        improvements = ["Add a specific example and say what changed because of your work."]
    if len(better) < 40 or _has_new_number(better, allowed_numbers) or _claims_hiring_job(better, hiring_company, known_companies):
        better_answer = None
    else:
        better_answer = better[:900]
    return {
        "score": score,
        "summary": summary[:500],
        "strengths": strengths[:3],
        "improvements": improvements[:3],
        "better_answer": better_answer,
        "source": "model",
    }


def _rubric(answer: str, facts: dict[str, Any]) -> dict[str, Any]:
    skills = [str(item).lower() for item in (facts.get("skills") or [])]
    companies = [
        str(role.get("company") or "").lower()
        for role in (facts.get("experience") or [])
        if isinstance(role, dict) and str(role.get("company") or "").strip()
    ]
    lowered = answer.lower()
    grounded = any(skill and skill in lowered for skill in skills) or any(name and name in lowered for name in companies)
    if len(answer) < 40:
        score = 3
        summary = "The answer is too short to show how you handled the situation."
    elif grounded:
        score = 7
        summary = "The answer uses a real part of your background. It can still say what you did and what changed."
    else:
        score = 5
        summary = "The answer is on the topic, but it does not yet use a specific example from your background."
    return {
        "score": score,
        "summary": summary,
        "strengths": ["You answered in your own words."] if len(answer) >= 40 else [],
        "improvements": ["Name the situation, what you did, and the result."],
        "better_answer": None,
        "source": "rubric",
    }


def _copy_facts(facts: dict[str, Any]) -> dict[str, Any]:
    profile = dict(facts.get("profile") or {})
    return {
        "profile": {
            "full_name": _or_none(profile.get("full_name")),
            "location": _or_none(profile.get("location")),
            "summary": _or_none(profile.get("summary")),
        },
        "skills": _clean_list(facts.get("skills") or []),
        "experience": [_clean_role(item) for item in (facts.get("experience") or []) if isinstance(item, dict)],
        "education": [dict(item) for item in (facts.get("education") or []) if isinstance(item, dict)],
        "projects": [_clean_project(item) for item in (facts.get("projects") or []) if isinstance(item, dict)],
        "certifications": _clean_list(facts.get("certifications") or []),
        "achievements": _clean_list(facts.get("achievements") or []),
    }


def _session_facts(cv: dict[str, Any]) -> dict[str, Any]:
    profile = cv.get("profile") or {}
    roles = []
    for role in (cv.get("experience") or [])[:4]:
        roles.append(
            {
                "title": role.get("title"),
                "company": role.get("company"),
                "bullets": [str(item)[:180] for item in (role.get("bullets") or [])[:2]],
            }
        )
    return {
        "full_name": profile.get("full_name"),
        "summary": _clip(str(profile.get("summary") or ""), 400) or None,
        "skills": list(cv.get("skills") or [])[:20],
        "experience": roles,
        "projects": list(cv.get("projects") or [])[:3],
        "certifications": list(cv.get("certifications") or [])[:8],
        "achievements": list(cv.get("achievements") or [])[:6],
    }


def _prompt_facts(cv: dict[str, Any], title: str, company: str, location: str, description: str) -> str:
    payload = {
        "target_title": title or None,
        "hiring_company": company or None,
        "job_location": location or None,
        "job_description": _clip(description, 1200) or None,
        "candidate": _session_facts(cv),
    }
    return json.dumps(payload, ensure_ascii=False)


def _extract_json(text: str) -> dict[str, Any] | None:
    cleaned = _strip_think(text)
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned.strip(), flags=re.I)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _clean_project(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _or_none(item.get("name")),
        "description": _clip(str(item.get("description") or ""), 240) or None,
    }


def _clean_role(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": _or_none(item.get("title")),
        "company": _or_none(item.get("company")),
        "bullets": _as_bullets(item.get("bullets")),
    }


def _as_bullets(value: Any) -> list[str]:
    rows = value if isinstance(value, list) else [value]
    found = []
    for item in rows:
        text = re.sub(r"^[\-\u2022•]+\s*", "", str(item or "").strip())
        text = re.sub(r"\s+", " ", text).strip(" \"'")
        if len(text) >= 12:
            found.append(text[:280])
    return found


def _clean_list(values: Any) -> list[str]:
    found = []
    seen: set[str] = set()
    for item in values or []:
        text = re.sub(r"\s+", " ", str(item or "").strip())
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        found.append(text)
    return found


def _short_list(value: Any) -> list[str]:
    rows = value if isinstance(value, list) else []
    found = []
    for item in rows:
        text = _one_line(str(item or ""))
        if len(text) < 8:
            continue
        found.append(text[:240])
        if len(found) == 3:
            break
    return found


def _score(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        return None
    if 1 <= number <= 10:
        return number
    return None


def _claims_hiring_job(text: str, hiring_company: str, known_companies: set[str]) -> bool:
    company = (hiring_company or "").strip()
    if not company or company.lower() in known_companies:
        return False
    pattern = _EMPLOYED_RE.pattern.format(name=re.escape(company))
    return re.search(pattern, text) is not None


def _number_keys(text: str) -> set[str]:
    return {re.sub(r"[\s$%,]", "", raw) for raw in _NUMBER_RE.findall(text or "")}


def _has_new_number(text: str, allowed: set[str]) -> bool:
    return any(key not in allowed for key in _number_keys(text))


def _one_line(text: str) -> str:
    cleaned = _strip_think(text)
    cleaned = re.sub(r"^[\-•]\s*", "", cleaned).strip()
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def _strip_think(text: str) -> str:
    cleaned = re.sub(r"(?is)<think>.*?</think>", "", text or "")
    if re.search(r"(?i)</think>", cleaned):
        cleaned = re.split(r"(?i)</think>", cleaned)[-1]
    return cleaned.strip()


def _clip(text: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rsplit(" ", 1)[0]


def _or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
