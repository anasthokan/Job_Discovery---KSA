"""Write a cover letter with Ollama from facts the caller already supplied.

The greeting and signature come from the request. The model may write the
paragraphs. Employers and numbers that were not supplied are rejected, and a
letter built from the facts is returned instead.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from jobscraper.jd_to_cv import parse_job_description
from jobscraper.ollama_resume import OllamaError, active_model, complete

_SYSTEM = """You write a job application cover letter from facts the user already supplied.
Return one JSON object and nothing else. No markdown.

Rules:
- First person. Use "I".
- paragraphs: exactly 3 strings. Each paragraph is 2 to 4 sentences.
- Paragraph 1 says which role and company the person is applying for.
- Paragraph 2 uses only employers, titles, skills, and numbers present in FACTS.
- Paragraph 3 asks to discuss the role. No salary and no visa claims.
- Do not add employers, tools, degrees, certifications, or metrics.
- Do not say the person already works at the hiring company unless that company is in FACTS.
- Do not invent a hiring manager name.
- Keys: paragraphs.
"""

_NUMBER_RE = re.compile(r"(?:\$\s*)?\d[\d,]*(?:\.\d+)?%?")
_EMPLOYED_RE = re.compile(
    r"(?i)\b(?:i am|i'm|i)\s+(?:currently\s+|already\s+)?(?:work|working|worked|employed)\w*"
    r"\s+(?:at|for|with)\s+{name}\b"
)

Writer = Callable[..., str]


class CoverLetterError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def generate_cover_letter(
    facts: dict[str, Any],
    *,
    job: dict[str, Any] | None = None,
    target_title: str = "",
    hiring_manager: str = "",
    writer: Writer | None = None,
) -> dict[str, Any]:
    """Return a cover letter plus plain text. Raises CoverLetterError."""
    cv = _copy_facts(facts)
    if not _has_facts(cv):
        raise CoverLetterError(
            "Send skills, work history, education, or a summary. "
            "This API writes from those facts and does not invent a career.",
            400,
        )

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
        raise CoverLetterError(
            "Send a job title, company, job description, or job_id so the letter has a role to address.",
            400,
        )

    manager = _clean_manager(hiring_manager)
    prompt = _SYSTEM + "\n\nFACTS:\n" + _prompt_facts(cv, title, company, location, description, manager)
    try:
        raw, model_name = _write(prompt, writer)
    except OllamaError as exc:
        raise CoverLetterError(str(exc), 503) from exc

    generated = _extract_json(raw)
    allowed_numbers = _number_keys(json.dumps(cv, ensure_ascii=False))
    known_companies = {
        str(role.get("company") or "").strip().lower()
        for role in (cv.get("experience") or [])
        if str(role.get("company") or "").strip()
    }
    paragraphs = _paragraphs_from_model(
        generated.get("paragraphs"),
        allowed_numbers,
        company,
        known_companies,
    )
    source = "model"
    if paragraphs is None:
        paragraphs = _fallback_paragraphs(cv, title, company)
        source = "facts"

    letter = {
        "greeting": _greeting(manager),
        "paragraphs": paragraphs,
        "closing": "Sincerely,",
        "signature": _or_none((cv.get("profile") or {}).get("full_name")),
    }
    job_view = _job_view(title, company, location, description)
    return {
        "ok": True,
        "mode": "generated",
        "model": model_name,
        "letter_source": source,
        "target_title": title or None,
        "hiring_manager": manager or None,
        "note": (
            "The greeting uses the hiring manager you sent, or Dear Hiring Manager. "
            "Employers and numbers that were not in the request are left out. "
            "The wording is first person."
        ),
        "job": job_view,
        "letter": letter,
        "letter_text": render_cover_letter(cv, letter),
    }


def render_cover_letter(cv: dict[str, Any], letter: dict[str, Any]) -> str:
    """Plain text the PDF and Word downloads use. The first line is the name."""
    profile = cv.get("profile") or {}
    contact = [
        value
        for value in (profile.get("location"), profile.get("email"), profile.get("phone"))
        if value
    ]
    lines = [profile.get("full_name") or "Applicant"]
    if contact:
        lines.append(" | ".join(contact))
    lines.append("")
    lines.append(str(letter.get("greeting") or "Dear Hiring Manager,"))
    lines.append("")
    for paragraph in letter.get("paragraphs") or []:
        lines.append(str(paragraph).strip())
        lines.append("")
    lines.append(str(letter.get("closing") or "Sincerely,"))
    if letter.get("signature"):
        lines.append(str(letter["signature"]))
    return "\n".join(lines).strip() + "\n"


def _write(prompt: str, writer: Writer | None) -> tuple[str, str]:
    if writer is not None:
        return writer(prompt), "test"
    text = complete(
        "Return only the JSON object described in the user message.",
        prompt,
        temperature=0.3,
        num_predict=420,
        timeout=75,
    )
    return text, active_model() or "ollama"


def _copy_facts(facts: dict[str, Any]) -> dict[str, Any]:
    profile = dict(facts.get("profile") or {})
    return {
        "profile": {
            "full_name": _or_none(profile.get("full_name")),
            "email": _or_none(profile.get("email")),
            "phone": _or_none(profile.get("phone")),
            "location": _or_none(profile.get("location")),
            "summary": _or_none(profile.get("summary")),
        },
        "skills": _clean_list(facts.get("skills") or []),
        "experience": [_clean_role(item) for item in (facts.get("experience") or []) if isinstance(item, dict)],
        "education": [dict(item) for item in (facts.get("education") or []) if isinstance(item, dict)],
        "certifications": _clean_list(facts.get("certifications") or []),
        "projects": [dict(item) for item in (facts.get("projects") or []) if isinstance(item, dict)],
    }


def _has_facts(cv: dict[str, Any]) -> bool:
    profile = cv.get("profile") or {}
    summary = str(profile.get("summary") or "").strip()
    return bool(
        cv.get("skills")
        or cv.get("experience")
        or cv.get("education")
        or cv.get("certifications")
        or cv.get("projects")
        or len(summary) >= 40
    )


def _prompt_facts(
    cv: dict[str, Any],
    title: str,
    company: str,
    location: str,
    description: str,
    manager: str,
) -> str:
    profile = cv.get("profile") or {}
    roles = []
    for role in (cv.get("experience") or [])[:6]:
        roles.append(
            {
                "title": role.get("title"),
                "company": role.get("company"),
                "start_date": role.get("start_date"),
                "end_date": role.get("end_date"),
                "bullets": [str(item)[:240] for item in (role.get("bullets") or [])[:4]],
            }
        )
    payload = {
        "target_title": title or None,
        "hiring_company": company or None,
        "job_location": location or None,
        "hiring_manager": manager or None,
        "job_description": _clip(description, 1500) or None,
        "full_name": profile.get("full_name"),
        "candidate_location": profile.get("location"),
        "existing_summary": _clip(str(profile.get("summary") or ""), 600) or None,
        "skills": list(cv.get("skills") or [])[:30],
        "experience": roles,
        "education": cv.get("education") or [],
        "certifications": cv.get("certifications") or [],
    }
    return json.dumps(payload, ensure_ascii=False)


def _paragraphs_from_model(
    generated: Any,
    allowed_numbers: set[str],
    hiring_company: str,
    known_companies: set[str],
) -> list[str] | None:
    rows = generated if isinstance(generated, list) else []
    accepted: list[str] = []
    for item in rows:
        paragraph = _one_paragraph(str(item or ""))
        if len(paragraph) < 80:
            return None
        if _has_new_number(paragraph, allowed_numbers):
            return None
        if _claims_hiring_job(paragraph, hiring_company, known_companies):
            return None
        accepted.append(paragraph[:900])
        if len(accepted) == 3:
            return accepted
    return None


def _claims_hiring_job(text: str, hiring_company: str, known_companies: set[str]) -> bool:
    company = (hiring_company or "").strip()
    if not company or company.lower() in known_companies:
        return False
    pattern = _EMPLOYED_RE.pattern.format(name=re.escape(company))
    return re.search(pattern, text) is not None


def _fallback_paragraphs(cv: dict[str, Any], title: str, company: str) -> list[str]:
    profile = cv.get("profile") or {}
    skills = list(cv.get("skills") or [])[:5]
    roles = [role for role in (cv.get("experience") or []) if isinstance(role, dict)]
    if title and company:
        opening = f"I am writing to apply for the {title} role at {company}."
    elif title:
        opening = f"I am writing to apply for the {title} role."
    elif company:
        opening = f"I am writing to apply for the open role at {company}."
    else:
        opening = "I am writing to apply for the role described in your posting."
    if profile.get("location"):
        opening += f" I am based in {profile['location']}."
    if skills:
        opening += f" My background includes {', '.join(skills)}."

    role = roles[0] if roles else {}
    role_title = str(role.get("title") or "").strip()
    role_company = str(role.get("company") or "").strip()
    bullets = [str(item).strip() for item in (role.get("bullets") or []) if str(item).strip()]
    if role_title and role_company:
        middle = f"I worked as {role_title} at {role_company}."
        if bullets:
            middle += " " + bullets[0].rstrip(".") + "."
    elif skills:
        middle = f"I can apply {', '.join(skills[:4])} to the work this role describes."
    else:
        summary = str(profile.get("summary") or "").strip()
        middle = summary if len(summary) >= 40 else "The experience in my application is the experience I would bring to this role."

    role_name = title or "this role"
    where = f" at {company}" if company else ""
    closing = (
        f"I would welcome the chance to discuss how this background fits the {role_name} opening{where}. "
        "Thank you for considering my application."
    )
    return [opening, middle, closing]


def _greeting(manager: str) -> str:
    if manager:
        return f"Dear {manager},"
    return "Dear Hiring Manager,"


def _clean_manager(value: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").replace("\n", " ")).strip(" ,")
    if len(text) < 2:
        return ""
    return text[:80]


def _job_view(title: str, company: str, location: str, description: str) -> dict[str, Any]:
    view: dict[str, Any] = {
        "title": title or None,
        "company": company or None,
        "location": location or None,
    }
    if len(description) >= 40:
        parsed = parse_job_description(
            description,
            title=title or None,
            company=company or None,
            location=location or None,
        )
        view["title"] = title or parsed.get("title")
        view["company"] = company or parsed.get("company")
        view["location"] = location or parsed.get("location")
    return view


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = _strip_think(text)
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned.strip(), flags=re.I)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise CoverLetterError("The model did not return cover letter JSON.", 502)
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise CoverLetterError("The model returned invalid cover letter JSON.", 502) from exc
    if not isinstance(parsed, dict):
        raise CoverLetterError("The model returned invalid cover letter JSON.", 502)
    return parsed


def _clean_role(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": _or_none(item.get("title")),
        "company": _or_none(item.get("company")),
        "start_date": _or_none(item.get("start_date")),
        "end_date": _or_none(item.get("end_date")),
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


def _number_keys(text: str) -> set[str]:
    return {re.sub(r"[\s$%,]", "", raw) for raw in _NUMBER_RE.findall(text or "")}


def _has_new_number(text: str, allowed: set[str]) -> bool:
    return any(key not in allowed for key in _number_keys(text))


def _one_paragraph(text: str) -> str:
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
