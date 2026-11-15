"""Write a resume with Ollama from facts the caller already supplied.

Employers, titles, dates, skills, education, and certifications stay as given.
The model may rewrite the summary and experience bullets only.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from jobscraper.jd_to_cv import parse_job_description
from jobscraper.ollama_resume import OllamaError, active_model, complete

_SYSTEM = """You write resume wording from facts the user already supplied.
Return one JSON object and nothing else. No markdown.

Rules:
- Use only employers, titles, dates, skills, schools, and numbers present in FACTS.
- Do not add employers, degrees, certifications, tools, or metrics.
- Do not say the person works at the hiring company unless that company is already in FACTS.
- summary: 5 to 6 sentences. Third person. No "I". Name the target job title once.
- key_achievements: 4 to 6 bullets. Each bullet restates a supplied experience bullet or applies a supplied skill. No new numbers.
- experience: same roles, same order, same company and title strings. 4 to 6 bullets each, covering the supplied skills that fit the role. No new numbers.
- projects: include 2 or 3 only when FRESHER is true or FACTS already lists projects. Keep a supplied project name. If FRESHER is true and no project was supplied, name each project after a supplied skill. No clients and no metrics.
- skills: reorder the given skills so the most relevant come first. Do not add skills.
- Do not return education or certifications.
- Keys: summary, skills, key_achievements, experience (array of {company, title, bullets}), projects (array of {name, description}).
"""

_NUMBER_RE = re.compile(r"(?:\$\s*)?\d[\d,]*(?:\.\d+)?%?")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")

Writer = Callable[..., str]


class ResumeGenerateError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def generate_resume(
    facts: dict[str, Any],
    *,
    job: dict[str, Any] | None = None,
    target_title: str = "",
    writer: Writer | None = None,
) -> dict[str, Any]:
    """Return a resume JSON document plus plain text. Raises ResumeGenerateError."""
    cv = _copy_facts(facts)
    if not _has_facts(cv):
        raise ResumeGenerateError(
            "Send skills, work history, education, or a summary. "
            "This API writes from those facts and does not invent a career.",
            400,
        )

    posting = job or {}
    title = (target_title or "").strip() or str(posting.get("title") or "").strip()
    if not title:
        roles = cv.get("experience") or []
        title = str((roles[0] or {}).get("title") or "").strip() if roles else ""
    description = str(posting.get("description") or "").strip()
    company = str(posting.get("company") or "").strip()
    location = str(posting.get("location") or "").strip()

    prompt = _SYSTEM + "\n\nFACTS:\n" + _prompt_facts(cv, title, company, description)
    try:
        raw, model_name = _write(prompt, writer)
    except OllamaError as exc:
        raise ResumeGenerateError(str(exc), 503) from exc

    generated = _extract_json(raw)
    allowed_numbers = _number_keys(json.dumps(cv, ensure_ascii=False))
    span = _year_span(cv.get("experience") or [])
    if span:
        allowed_numbers.add(str(span))

    summary, summary_source = _summary_from_model(
        str(generated.get("summary") or ""),
        cv,
        title,
        company,
        allowed_numbers,
    )
    original_roles = [dict(role) for role in (cv.get("experience") or [])]
    experience, bullets_changed = _experience_from_model(
        cv.get("experience") or [],
        generated.get("experience") or [],
        hiring_company=company,
    )
    skills = _order_skills(list(cv.get("skills") or []), generated.get("skills") or [])
    experience = _pad_experience(experience, skills, title)
    fresher = _is_fresher(original_roles)
    achievements = _achievements_from_model(
        generated.get("key_achievements"),
        cv.get("achievements") or [],
        allowed_numbers,
        company,
    )
    if len(achievements) < 4:
        achievements = _fill_achievements(achievements, original_roles, skills, title)
    projects = _projects_from_model(
        cv.get("projects") or [],
        generated.get("projects"),
        skills,
        title,
        fresher=fresher,
        allowed_numbers=allowed_numbers,
        hiring_company=company,
    )
    cv["profile"]["summary"] = summary
    cv["experience"] = experience
    cv["skills"] = skills
    cv["achievements"] = achievements
    cv["projects"] = projects

    job_view = _job_view(title, company, location, description)
    return {
        "ok": True,
        "mode": "generated",
        "model": model_name,
        "target_title": title,
        "summary_source": summary_source,
        "rewritten": summary_source == "model" or bullets_changed,
        "fresher": fresher,
        "note": (
            "Employers, dates, skills, education, and certifications come from the request. "
            "Key achievements and professional experience are written from those facts. "
            "Projects are included for freshers."
        ),
        "job": job_view,
        "match": _match(skills, description, job_view.get("required_skills") or []),
        "cv": cv,
        "cv_text": render_resume_text(cv),
    }


def _write(prompt: str, writer: Writer | None) -> tuple[str, str]:
    if writer is not None:
        return writer(prompt), "test"
    text = complete(
        "Return only the JSON object described in the user message.",
        prompt,
        temperature=0.2,
        num_predict=2200,
        timeout=120,
    )
    return text, active_model() or "ollama"


def _copy_facts(facts: dict[str, Any]) -> dict[str, Any]:
    profile = dict(facts.get("profile") or {})
    return {
        "profile": {
            "full_name": _or_none(profile.get("full_name")),
            "email": _or_none(profile.get("email")),
            "phone": _or_none(profile.get("phone")),
            "phones": [str(item) for item in (profile.get("phones") or []) if str(item).strip()],
            "location": _or_none(profile.get("location")),
            "linkedin": _or_none(profile.get("linkedin")),
            "github": _or_none(profile.get("github")),
            "website": _or_none(profile.get("website")),
            "summary": _or_none(profile.get("summary")),
        },
        "skills": _clean_list(facts.get("skills") or []),
        "experience": [_clean_role(item) for item in (facts.get("experience") or []) if isinstance(item, dict)],
        "education": [dict(item) for item in (facts.get("education") or []) if isinstance(item, dict)],
        "projects": [dict(item) for item in (facts.get("projects") or []) if isinstance(item, dict)],
        "certifications": _cert_names(facts.get("certifications") or []),
        "languages": _clean_list(facts.get("languages") or []),
        "achievements": _as_bullets(facts.get("achievements") or []),
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


def _prompt_facts(cv: dict[str, Any], title: str, company: str, description: str) -> str:
    profile = cv.get("profile") or {}
    roles = []
    for role in (cv.get("experience") or [])[:8]:
        roles.append(
            {
                "title": role.get("title"),
                "company": role.get("company"),
                "location": role.get("location"),
                "start_date": role.get("start_date"),
                "end_date": role.get("end_date"),
                "bullets": [str(item)[:240] for item in (role.get("bullets") or [])[:6]],
            }
        )
    payload = {
        "target_title": title or None,
        "hiring_company": company or None,
        "job_description": _clip(description, 1500) or None,
        "full_name": profile.get("full_name"),
        "location": profile.get("location"),
        "existing_summary": _clip(str(profile.get("summary") or ""), 800) or None,
        "skills": list(cv.get("skills") or [])[:40],
        "experience": roles,
        "education": cv.get("education") or [],
        "certifications": cv.get("certifications") or [],
        "projects": [
            {
                "name": item.get("name"),
                "description": _clip(str(item.get("description") or ""), 300) or None,
            }
            for item in (cv.get("projects") or [])[:6]
            if isinstance(item, dict)
        ],
        "languages": cv.get("languages") or [],
        "key_achievements": list(cv.get("achievements") or [])[:6],
        "fresher": _is_fresher(cv.get("experience") or []),
    }
    return json.dumps(payload, ensure_ascii=False)


def _summary_from_model(
    text: str,
    cv: dict[str, Any],
    title: str,
    hiring_company: str,
    allowed_numbers: set[str],
) -> tuple[str, str]:
    summary = _one_paragraph(text)
    known = {
        str(role.get("company") or "").strip().lower()
        for role in (cv.get("experience") or [])
        if str(role.get("company") or "").strip()
    }
    claims_hiring = bool(hiring_company) and hiring_company.lower() not in known and hiring_company.lower() in summary.lower()
    if len(summary) >= 60 and not _has_new_number(summary, allowed_numbers) and not claims_hiring:
        return summary[:900], "model"
    return _fallback_summary(cv, title), "facts"


def _experience_from_model(
    original: list[dict[str, Any]],
    generated: Any,
    *,
    hiring_company: str = "",
) -> tuple[list[dict[str, Any]], bool]:
    rows = generated if isinstance(generated, list) else []
    changed = False
    merged: list[dict[str, Any]] = []
    for index, role in enumerate(original):
        kept = dict(role)
        original_bullets = list(role.get("bullets") or [])
        source_blob = " ".join(
            [
                str(role.get("title") or ""),
                str(role.get("company") or ""),
                str(role.get("location") or ""),
                *original_bullets,
            ]
        ).lower()
        # A rewritten bullet may repeat a number already written for that role.
        # A role with no bullets may mention its own dates, and nothing else numeric.
        role_numbers = _number_keys(" ".join(original_bullets)) or _number_keys(
            " ".join(str(role.get(key) or "") for key in ("start_date", "end_date", "title", "company"))
        )
        incoming = rows[index] if index < len(rows) and isinstance(rows[index], dict) else {}
        forbidden = [
            name.lower()
            for name in (str(incoming.get("company") or "").strip(), hiring_company.strip())
            if name and name.lower() not in source_blob
        ]
        fresh = []
        for bullet in _as_bullets(incoming.get("bullets")):
            if _has_new_number(bullet, role_numbers):
                continue
            lowered = bullet.lower()
            if any(name in lowered for name in forbidden):
                continue
            fresh.append(bullet)
        if fresh and (not original_bullets or len(fresh) >= len(original_bullets)):
            kept["bullets"] = fresh[:6]
            changed = True
        merged.append(kept)
    return merged, changed


def _pad_experience(roles: list[dict[str, Any]], skills: list[str], target: str) -> list[dict[str, Any]]:
    """Give each job enough bullets to fill a professional experience section."""
    padded = []
    for role in roles:
        kept = dict(role)
        bullets = list(kept.get("bullets") or [])
        title = kept.get("title") or target or "this role"
        company = kept.get("company") or ""
        place = f" at {company}" if company else ""
        additions = []
        if skills:
            additions.append(f"Applied {', '.join(skills[:4])} in the {title} role{place}.")
        if len(skills) > 4:
            additions.append(f"Used {', '.join(skills[4:8])} to support the same {title} work{place}.")
        additions.append(f"Handled day-to-day {title} responsibilities{place}, from implementation through handover.")
        seen = {item.lower() for item in bullets}
        for item in additions:
            if len(bullets) >= 5:
                break
            if item.lower() in seen:
                continue
            bullets.append(item)
            seen.add(item.lower())
        kept["bullets"] = bullets[:6]
        padded.append(kept)
    return padded


def _is_fresher(experience: list[dict[str, Any]]) -> bool:
    if not experience:
        return True
    intern = re.compile(r"(?i)\b(intern|trainee|fresher|student)\b")
    if all(intern.search(str(role.get("title") or "")) for role in experience):
        return True
    span = _year_span(experience)
    return span is not None and span < 1


def _achievements_from_model(
    generated: Any,
    supplied: list[str],
    allowed_numbers: set[str],
    hiring_company: str,
) -> list[str]:
    source = _as_bullets(generated) or list(supplied)
    kept = []
    hiring = hiring_company.strip().lower()
    for bullet in source:
        if _has_new_number(bullet, allowed_numbers):
            continue
        if hiring and hiring not in " ".join(supplied).lower() and hiring in bullet.lower():
            continue
        kept.append(bullet)
    return _dedupe(kept)[:6]


def _fill_achievements(
    current: list[str],
    roles: list[dict[str, Any]],
    skills: list[str],
    target: str,
) -> list[str]:
    found = list(current)
    for role in roles:
        title = role.get("title") or target or "the role"
        company = role.get("company") or ""
        where = f" at {company}" if company else ""
        for bullet in role.get("bullets") or []:
            found.append(f"{str(bullet).rstrip('.')} while working as {title}{where}.")
    if skills:
        found.append(f"Demonstrated hands-on use of {', '.join(skills[:6])}.")
    if target and skills:
        found.append(f"Aligned {', '.join(skills[:3])} work with {target} responsibilities.")
    return _dedupe(found)[:6]


def _projects_from_model(
    original: list[dict[str, Any]],
    generated: Any,
    skills: list[str],
    target: str,
    *,
    fresher: bool,
    allowed_numbers: set[str],
    hiring_company: str,
) -> list[dict[str, Any]]:
    if not fresher and not original:
        return []
    incoming = generated if isinstance(generated, list) else []
    if original:
        merged = []
        for index, project in enumerate(original):
            kept = {
                "name": _or_none(project.get("name")) or "Project",
                "description": _or_none(project.get("description")),
            }
            row = incoming[index] if index < len(incoming) and isinstance(incoming[index], dict) else {}
            description = _one_paragraph(str(row.get("description") or ""))
            if (
                len(description) >= 20
                and not _has_new_number(description, allowed_numbers)
                and not (hiring_company and hiring_company.lower() not in str(kept.get("description") or "").lower() and hiring_company.lower() in description.lower())
            ):
                kept["description"] = description[:300]
            merged.append(kept)
        return merged
    accepted = []
    hiring = hiring_company.strip().lower()
    for row in incoming:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        description = _one_paragraph(str(row.get("description") or ""))
        if len(name) < 3 or len(description) < 20:
            continue
        if _has_new_number(f"{name} {description}", allowed_numbers):
            continue
        if hiring and hiring in f"{name} {description}".lower():
            continue
        accepted.append({"name": name[:80], "description": description[:300]})
    if accepted:
        return accepted[:3]
    return _fallback_projects(skills, target)


def _fallback_projects(skills: list[str], target: str) -> list[dict[str, Any]]:
    if not skills:
        return []
    groups = [skills[:2]]
    if len(skills) > 2:
        groups.append(skills[2:4] or skills[:1])
    projects = []
    for group in groups:
        if not group:
            continue
        projects.append(
            {
                "name": f"{group[0]} Project",
                "description": (
                    f"Personal project applying {', '.join(group)} toward {target or 'software'} work, "
                    "covering design, implementation, and a short demo."
                ),
            }
        )
    return projects[:3]


def render_resume_text(cv: dict[str, Any]) -> str:
    """Resume text with the sections the PDF download shows."""
    profile = cv.get("profile") or {}
    contact = [value for value in (profile.get("location"), profile.get("email"), profile.get("phone")) if value]
    links = [value for value in (profile.get("linkedin"), profile.get("github"), profile.get("website")) if value]
    parts = ["\n".join(bit for bit in [profile.get("full_name") or "[Your Name]", " | ".join(contact), " | ".join(links)] if bit)]
    if profile.get("summary"):
        parts.append("SUMMARY\n" + profile["summary"])
    if cv.get("skills"):
        parts.append("SKILLS\n" + ", ".join(cv["skills"]))
    if cv.get("achievements"):
        parts.append("KEY ACHIEVEMENTS\n" + "\n".join(f"- {item}" for item in cv["achievements"]))
    if cv.get("experience"):
        blocks = ["PROFESSIONAL EXPERIENCE"]
        for job in cv["experience"]:
            meta = " | ".join(value for value in (job.get("title"), job.get("company"), job.get("location")) if value)
            dates = " - ".join(value for value in (job.get("start_date"), job.get("end_date")) if value)
            if meta:
                blocks.append(meta)
            if dates:
                blocks.append(dates)
            for bullet in job.get("bullets") or []:
                blocks.append(f"- {bullet}")
            blocks.append("")
        parts.append("\n".join(blocks).strip())
    if cv.get("projects"):
        blocks = ["PROJECTS"]
        for project in cv["projects"]:
            blocks.append(project.get("name") or "Project")
            if project.get("description"):
                blocks.append(f"- {project['description']}")
        parts.append("\n".join(blocks))
    if cv.get("certifications"):
        parts.append("CERTIFICATIONS\n" + "\n".join(f"- {item}" for item in cv["certifications"]))
    education = _education_lines(cv.get("education") or [])
    if education:
        parts.append("EDUCATION\n" + "\n".join(education))
    if cv.get("languages"):
        parts.append("LANGUAGES\n" + ", ".join(cv["languages"]))
    return "\n\n".join(parts).strip() + "\n"


def _education_lines(items: list[dict[str, Any]]) -> list[str]:
    lines = []
    for item in items:
        if not isinstance(item, dict):
            continue
        degree = " ".join(value for value in (item.get("degree"), item.get("field")) if value).strip()
        years = " - ".join(value for value in (item.get("start_year"), item.get("end_year")) if value)
        line = " | ".join(value for value in (degree or None, item.get("institution"), years or None) if value)
        gpa = str(item.get("gpa") or "").strip()
        if gpa:
            line = f"{line} | GPA {gpa}" if line else f"GPA {gpa}"
        if line:
            lines.append(line)
    return lines


def _cert_names(values: Any) -> list[str]:
    names = []
    for item in values or []:
        if isinstance(item, dict):
            text = " — ".join(
                part
                for part in (str(item.get("name") or item.get("title") or "").strip(), str(item.get("issuer") or "").strip())
                if part
            )
        else:
            text = str(item or "").strip()
        if text:
            names.append(text)
    return _clean_list(names)


def _dedupe(values: list[str]) -> list[str]:
    found = []
    seen: set[str] = set()
    for item in values:
        text = re.sub(r"\s+", " ", str(item or "").strip())
        key = text.lower()
        if len(text) < 12 or key in seen:
            continue
        seen.add(key)
        found.append(text)
    return found


def _order_skills(original: list[str], suggested: Any) -> list[str]:
    by_key = {item.lower(): item for item in original}
    ordered: list[str] = []
    seen: set[str] = set()
    for item in _as_strings(suggested):
        key = item.lower()
        if key in by_key and key not in seen:
            ordered.append(by_key[key])
            seen.add(key)
    for item in original:
        key = item.lower()
        if key not in seen:
            ordered.append(item)
            seen.add(key)
    return ordered


def _fallback_summary(cv: dict[str, Any], title: str) -> str:
    profile = cv.get("profile") or {}
    name = profile.get("full_name") or "This candidate"
    role = title or "professional"
    skills = ", ".join((cv.get("skills") or [])[:5])
    sentence = f"{name} is a {role}"
    span = _year_span(cv.get("experience") or [])
    if span:
        sentence += f" with {span} years of experience"
    if skills:
        sentence += f" in {skills}"
    sentence += "."
    employers = [str(item.get("company")) for item in (cv.get("experience") or []) if item.get("company")]
    if employers:
        sentence += f" Recent work includes {employers[0]}."
    existing = str(profile.get("summary") or "").strip()
    if len(existing) >= 40:
        return existing
    return sentence


def _job_view(title: str, company: str, location: str, description: str) -> dict[str, Any]:
    view: dict[str, Any] = {
        "title": title or None,
        "company": company or None,
        "location": location or None,
        "employment_type": None,
        "workplace_type": None,
        "required_skills": [],
        "preferred_skills": [],
        "responsibilities": [],
        "qualifications": [],
        "keywords": [],
    }
    if len(description) >= 40:
        parsed = parse_job_description(description, title=title or None, company=company or None, location=location or None)
        view.update(parsed)
        if title:
            view["title"] = title
    return view


def _match(skills: list[str], description: str, required: list[str]) -> dict[str, Any]:
    user = {item.lower(): item for item in skills}
    if required:
        matched = [user[item.lower()] for item in required if item.lower() in user]
        missing = [item for item in required if item.lower() not in user]
        score = round(100 * len(matched) / len(required))
    elif description:
        blob = description.lower()
        matched = [item for item in skills if item.lower() in blob]
        missing = []
        score = None
    else:
        return {
            "score": None,
            "matched_skills": [],
            "missing_skills": [],
            "extra_skills": [],
        }
    matched_keys = {item.lower() for item in matched}
    extra = [item for item in skills if item.lower() not in matched_keys]
    return {
        "score": score,
        "matched_skills": matched[:40],
        "missing_skills": missing[:20],
        "extra_skills": extra[:20],
    }


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = _strip_think(text)
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned.strip(), flags=re.I)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ResumeGenerateError("The model did not return resume JSON.", 502)
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ResumeGenerateError("The model returned invalid resume JSON.", 502) from exc
    if not isinstance(parsed, dict):
        raise ResumeGenerateError("The model returned invalid resume JSON.", 502)
    return parsed


def _clean_role(item: dict[str, Any]) -> dict[str, Any]:
    end = _or_none(item.get("end_date"))
    return {
        "title": _or_none(item.get("title")),
        "company": _or_none(item.get("company")),
        "location": _or_none(item.get("location")),
        "start_date": _or_none(item.get("start_date")),
        "end_date": end,
        "is_current": bool(item.get("is_current")) or (end or "").lower() in {"present", "current"},
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


def _as_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,;\n]", value)
    if not isinstance(value, list):
        return []
    return _clean_list(value)


def _clean_list(values: Any) -> list[str]:
    found = []
    seen: set[str] = set()
    for item in values:
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


def _year_span(experience: list[dict[str, Any]]) -> int | None:
    years = [int(match) for role in experience for match in _YEAR_RE.findall(
        f"{role.get('start_date') or ''} {role.get('end_date') or ''}"
    )]
    if len(years) < 2:
        return None
    span = max(years) - min(years)
    if span < 1 or span > 60:
        return None
    return span


def _one_paragraph(text: str) -> str:
    cleaned = _strip_think(text)
    cleaned = re.sub(r"^```[a-zA-Z0-9]*\n|\n```$", "", cleaned).strip()
    cleaned = re.sub(r"(?i)^(professional summary|summary|here is|here's)\s*:?\s*", "", cleaned).strip()
    lines = [re.sub(r"^[\-•]\s*", "", line).strip() for line in cleaned.splitlines() if line.strip()]
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
