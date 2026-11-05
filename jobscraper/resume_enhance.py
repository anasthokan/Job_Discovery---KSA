"""Tailor a saved user profile to one job without inventing experience."""

from __future__ import annotations

from typing import Any

from jobscraper.jd_to_cv import JobToCvError, build_cv_from_job
from jobscraper.resume_parser import ResumeParseError, parse_resume_text


def enhance_profile_for_job(profile: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """Reorder the user's own resume toward this job and return cv text plus match."""
    resume = resume_from_profile(profile)
    if not _has_content(resume):
        raise JobToCvError("This user has no resume, skills, or work history to enhance.")
    payload = build_cv_from_job(
        str(job.get("description") or ""),
        job_title=job.get("title"),
        company=job.get("company"),
        location=job.get("location"),
        job_skills=list(job.get("skills") or []) or None,
        resume=resume,
        include_cv_text=True,
    )
    if payload.get("mode") != "tailored" or not (payload.get("cv_text") or "").strip():
        raise JobToCvError("This user has no resume content to enhance for this job.")
    return payload


def resume_from_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Shape a stored user profile into the resume JSON the tailor expects."""
    parsed = _parsed_resume_text(profile.get("resume_text") or "")
    parsed_profile = (parsed or {}).get("profile") or {}
    name = _join_name(profile.get("first_name"), profile.get("last_name")) or parsed_profile.get("full_name")
    location = _join_place(profile.get("city"), profile.get("state"), profile.get("country")) or parsed_profile.get("location")
    history = _experience_from_history(profile.get("work_history") or [])
    parsed_jobs = list((parsed or {}).get("experience") or [])
    experience = _richer_experience(history, parsed_jobs)
    schools = _education_from_profile(profile.get("education") or [])
    if not schools and parsed:
        schools = list(parsed.get("education") or [])
    summary = parsed_profile.get("summary")
    if not summary:
        text = str(profile.get("resume_text") or "").strip()
        if text and parsed is None:
            summary = text[:600]
    return {
        "profile": {
            "full_name": name,
            "email": _or_none(profile.get("email")) or parsed_profile.get("email"),
            "phone": _or_none(profile.get("phone")) or parsed_profile.get("phone"),
            "phones": [],
            "location": location,
            "linkedin": _or_none(profile.get("linkedin_url")) or parsed_profile.get("linkedin"),
            "github": _or_none(profile.get("github_url")) or parsed_profile.get("github"),
            "website": _or_none(profile.get("portfolio_url") or profile.get("personal_website")) or parsed_profile.get("website"),
            "summary": summary,
        },
        "skills": _unique([*(profile.get("skills") or []), *((parsed or {}).get("skills") or [])]),
        "experience": experience,
        "education": schools,
        "projects": list((parsed or {}).get("projects") or []),
        "certifications": _unique([*(profile.get("certifications") or []), *((parsed or {}).get("certifications") or [])]),
        "languages": list((parsed or {}).get("languages") or []),
    }


def _parsed_resume_text(text: str) -> dict[str, Any] | None:
    cleaned = str(text or "").strip()
    if len(cleaned) < 40:
        return None
    try:
        return parse_resume_text(cleaned)
    except ResumeParseError:
        return None


def _experience_from_history(rows: list[dict]) -> list[dict]:
    found = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("job_title") or row.get("title") or "").strip()
        company = str(row.get("company") or "").strip()
        if not title and not company:
            continue
        end = str(row.get("end_date") or "").strip()
        description = str(row.get("description") or row.get("responsibilities") or "").strip()
        bullets = [line.lstrip("-• ").strip() for line in description.splitlines() if line.strip()]
        found.append(
            {
                "title": title or None,
                "company": company or None,
                "location": None,
                "start_date": str(row.get("start_date") or "").strip() or None,
                "end_date": end or None,
                "is_current": end.lower() in {"", "present", "current"},
                "bullets": bullets[:12],
            }
        )
    return found


def _education_from_profile(rows: list[dict]) -> list[dict]:
    found = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        institution = str(row.get("school") or row.get("institution") or "").strip()
        degree = str(row.get("degree") or "").strip()
        field = str(row.get("field_of_study") or row.get("field") or "").strip()
        if not institution and not degree and not field:
            continue
        found.append(
            {
                "degree": degree or None,
                "field": field or None,
                "institution": institution or None,
                "start_year": None,
                "end_year": str(row.get("graduation_year") or row.get("end_year") or "").strip() or None,
                "gpa": str(row.get("gpa") or row.get("grade") or "").strip() or None,
            }
        )
    return found


def _richer_experience(history: list[dict], parsed_jobs: list[dict]) -> list[dict]:
    if _bullet_weight(parsed_jobs) > _bullet_weight(history):
        return parsed_jobs
    return history or parsed_jobs


def _bullet_weight(items: list[dict]) -> int:
    return sum(len(str(bullet)) for item in items for bullet in (item.get("bullets") or []))


def _has_content(resume: dict) -> bool:
    profile = resume.get("profile") or {}
    summary = str(profile.get("summary") or "").strip()
    return bool(
        resume.get("skills")
        or resume.get("experience")
        or resume.get("education")
        or resume.get("certifications")
        or resume.get("projects")
        or len(summary) >= 40
    )


def _join_name(*parts) -> str | None:
    text = " ".join(str(part).strip() for part in parts if str(part or "").strip())
    return text or None


def _join_place(*parts) -> str | None:
    text = ", ".join(str(part).strip() for part in parts if str(part or "").strip())
    return text or None


def _or_none(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def _unique(values) -> list[str]:
    found = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        found.append(text)
    return found
