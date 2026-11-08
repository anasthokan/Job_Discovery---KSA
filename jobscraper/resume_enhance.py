"""Tailor a saved user profile to one job without inventing experience."""

from __future__ import annotations

import re
from typing import Any

from jobscraper.jd_to_cv import JobToCvError, build_cv_from_job, render_cv_text
from jobscraper.resume_parser import ResumeParseError, parse_resume_text


_HEADING_LABELS = (
    "PROFESSIONAL SUMMARY",
    "CAREER OBJECTIVE",
    "KEY ACHIEVEMENTS",
    "PROFESSIONAL EXPERIENCE",
    "WORK EXPERIENCE",
    "WORK HISTORY",
    "CORE COMPETENCIES",
    "TECHNICAL SKILLS",
    "CORE SKILLS",
    "CERTIFICATIONS",
    "EDUCATION",
    "PROJECTS",
    "LANGUAGES",
    "SUMMARY",
    "EXPERIENCE",
    "SKILLS",
    "OBJECTIVE",
    "AWARDS",
    "PUBLICATIONS",
)
_HEADING_RE = re.compile(
    "(" + "|".join(re.escape(item) for item in sorted(_HEADING_LABELS, key=len, reverse=True)) + ")",
    re.I,
)
_HEADING_LINE_RE = re.compile(
    r"(?i)^(" + "|".join(re.escape(item) for item in _HEADING_LABELS) + r")$"
)
_UUID_RE = re.compile(r"\b[0-9a-f]{16,}\b", re.I)
_JOB_AD_RE = re.compile(
    r"(?is)^(.{0,500}?\$\s?\d[\d,\s\-–—]*\d?.{0,80}?(?:benefits|bonus)\s+)"
)
_DATE_RE = re.compile(
    r"(?i)\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}\b|\b(?:19|20)\d{2}\s*[-–—]\s*(?:present|current|now|(?:19|20)\d{2})\b"
)
_ROLE_RE = re.compile(
    r"(?i)\b(engineers?|developers?|managers?|analysts?|architects?|interns?|consultants?|specialists?|accountants?|registrars?|designers?|scientists?|administrators?)\b"
)


def enhance_profile_for_job(profile: dict[str, Any], job: dict[str, Any] | None = None) -> dict[str, Any]:
    """Download the user's own resume/profile as-is. Do not rewrite it for the job."""
    document = compose_resume_document(profile)
    if len(document.strip()) < 40:
        raise JobToCvError("This user has no resume or profile content to download.")
    resume = resume_from_profile({**profile, "resume_text": document, "resume_document": document})
    job = job or {}
    match = {
        "score": None,
        "matched_skills": [],
        "missing_skills": [],
        "extra_skills": [],
    }
    # Optional overlap report only. The file content stays the profile resume.
    if job.get("skills") or job.get("description") or job.get("title"):
        try:
            tailored = build_cv_from_job(
                str(job.get("description") or ""),
                job_title=job.get("title"),
                company=job.get("company"),
                location=job.get("location"),
                job_skills=list(job.get("skills") or []) or None,
                resume=resume,
                include_cv_text=False,
            )
            match = tailored.get("match") or match
        except JobToCvError:
            pass
    return {
        "ok": True,
        "mode": "profile",
        "job": {
            "title": job.get("title"),
            "company": job.get("company"),
            "location": job.get("location"),
        },
        "match": match,
        "cv": resume,
        "cv_text": document,
    }


def compose_resume_document(profile: dict[str, Any]) -> str:
    """Use the uploaded/saved resume as-is. Rebuild from profile only when that text is incomplete."""
    preserved = preserved_resume_text(profile)
    if _heading_count(preserved) >= 3 or _has_named_sections(preserved):
        return preserved
    rebuilt = rebuild_resume_from_profile(profile)
    if _section_richness(rebuilt) > _section_richness(preserved):
        return rebuilt
    return preserved or rebuilt


def preserved_resume_text(profile: dict[str, Any]) -> str:
    """Resume file text when we have it, otherwise the saved resume with job-ad junk removed."""
    file_text = str(profile.get("resume_document") or "").strip()
    saved = str(profile.get("resume_text") or "").strip()
    summary = str(profile.get("professional_summary") or "").strip()
    # Full uploaded resume first. Light-clean only when job-board junk is mixed in.
    candidates = []
    for raw in (file_text, saved, summary):
        if not raw:
            continue
        body = _restore_resume(raw) if _needs_resume_cleanup(raw) else _light_normalize(raw)
        if body:
            candidates.append(body)
    ranked = sorted(
        candidates,
        key=lambda item: (_heading_count(item), _has_named_sections(item), len(item)),
        reverse=True,
    )
    return ranked[0] if ranked else ""


def rebuild_resume_from_profile(profile: dict[str, Any]) -> str:
    """Build a sectioned resume from saved profile fields when the uploaded file is missing."""
    source = dict(profile)
    summary = str(profile.get("professional_summary") or "").strip()
    if summary and len(summary) >= 40:
        source["resume_text"] = summary
    resume = resume_from_profile(source)
    if summary and not (resume.get("profile") or {}).get("summary"):
        resume.setdefault("profile", {})["summary"] = summary
    if not _has_content(resume):
        return ""
    text = render_cv_text(resume)
    return _normalize_section_titles(text)


def _section_richness(text: str) -> tuple[int, int, int]:
    return (_heading_count(text), 1 if _has_named_sections(text) else 0, len(text or ""))


def _needs_resume_cleanup(text: str) -> bool:
    lowered = text.lower()
    if "$" in text and "bonus" in lowered:
        return True
    if "free plan monthly credits" in lowered or "mid-senior" in lowered:
        return True
    if _UUID_RE.search(text):
        return True
    title_lines = sum(
        1
        for line in text.splitlines()
        if _is_posting_title(re.sub(r"[ \t]{2,}", " ", line).strip())
    )
    return title_lines >= 3


def _light_normalize(text: str) -> str:
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    cleaned = cleaned.replace("\u2022", "- ").replace("•", "- ")
    cleaned = _separate_headings(cleaned)
    lines = [re.sub(r"[ \t]{2,}", " ", line).rstrip() for line in cleaned.splitlines()]
    lines = [line for line in lines if line.strip()]
    return "\n".join(lines).strip() + ("\n" if lines else "")


def _has_named_sections(text: str) -> bool:
    lowered = f"\n{(text or '').lower()}\n"
    needed = (
        "professional experience" in lowered or "work experience" in lowered or "\nexperience\n" in lowered,
        "\neducation\n" in lowered,
        "certification" in lowered or "competenc" in lowered or "\nskills\n" in lowered,
    )
    return sum(1 for item in needed if item) >= 2


def _normalize_section_titles(text: str) -> str:
    replacements = {
        r"(?im)^SUMMARY$": "PROFESSIONAL SUMMARY",
        r"(?im)^EXPERIENCE$": "PROFESSIONAL EXPERIENCE",
        r"(?im)^SKILLS$": "CORE COMPETENCIES",
    }
    out = text
    for pattern, title in replacements.items():
        out = re.sub(pattern, title, out)
    return out


def _restore_resume(text: str) -> str:
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    cleaned = cleaned.replace("\u2022", "- ").replace("•", "- ")
    cleaned = _separate_headings(cleaned)
    cleaned = _JOB_AD_RE.sub("", cleaned, count=1)
    lines = []
    started = False
    for raw in cleaned.splitlines():
        line = re.sub(r"[ \t]{2,}", " ", raw).strip()
        line = re.sub(r"(?i)\s*free plan monthly credits\.?", "", line).strip()
        if not line or _is_junk_line(line) or _is_job_ad_line(line) or line.lower() in {"benefits", "bonus"}:
            continue
        if not started and _is_posting_title(line):
            continue
        started = True
        lines.append(line)
    lines = _drop_posting_runs(lines)
    lines = _drop_repeated_lines(lines)
    lines = _drop_repeated_passages(lines)
    return "\n".join(lines).strip() + ("\n" if lines else "")


def _separate_headings(text: str) -> str:
    """Put every resume section title on its own line, even when two titles are glued together."""

    def place(match: re.Match[str]) -> str:
        raw = match.group(1)
        letters = [char for char in raw if char.isalpha()]
        upper = sum(1 for char in letters if char.isupper())
        mostly_caps = bool(letters) and upper / len(letters) >= 0.6
        at_line_start = match.start() == 0 or match.string[match.start() - 1] == "\n"
        rest_of_line = match.string[match.end() :].split("\n", 1)[0].strip()
        # A title in a sentence stays put. A real section title is its own line, or glued in capitals.
        if not mostly_caps and not (at_line_start and rest_of_line == ""):
            return raw
        return "\n" + raw.upper() + "\n"

    return _HEADING_RE.sub(place, text)


def _heading_count(text: str) -> int:
    return sum(1 for line in (text or "").splitlines() if _HEADING_LINE_RE.match(line.strip()))


def _is_junk_line(line: str) -> bool:
    lowered = line.lower().strip()
    if lowered in {"intermediate", "beginner", "advanced", "expert", "novice"}:
        return True
    if _UUID_RE.fullmatch(lowered):
        return True
    ids = _UUID_RE.findall(lowered)
    if len(ids) >= 2:
        return True
    return False


def _is_job_ad_line(line: str) -> bool:
    lowered = line.lower()
    if "$" in line and re.search(r"\d", line):
        return True
    if "discretionary bonus" in lowered or "mid-senior" in lowered:
        return True
    if "free plan monthly credits" in lowered:
        return True
    return False


def _is_posting_title(line: str) -> bool:
    """A bare job-board title, not a resume sentence, date, or bullet."""
    if _HEADING_LINE_RE.match(line) or _DATE_RE.search(line):
        return False
    if line.startswith("-") or len(line) > 140:
        return False
    words = line.split()
    if not (2 <= len(words) <= 16):
        return False
    return _ROLE_RE.search(line) is not None


def _drop_posting_runs(lines: list[str]) -> list[str]:
    """Drop a streak of saved job titles. One title next to a date or bullet stays."""
    kept: list[str] = []
    index = 0
    while index < len(lines):
        if not _is_posting_title(lines[index]):
            kept.append(lines[index])
            index += 1
            continue
        end = index
        while end < len(lines) and _is_posting_title(lines[end]):
            end += 1
        if end - index >= 3:
            index = end
            continue
        kept.extend(lines[index:end])
        index = end
    return kept


def _drop_repeated_lines(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for line in lines:
        key = re.sub(r"\s+", " ", line).strip().lower()
        if len(key) >= 40 and key in seen:
            continue
        if len(key) >= 40:
            seen.add(key)
        kept.append(line)
    return kept


def _drop_repeated_passages(lines: list[str]) -> list[str]:
    """Remove a second copy of the same paragraph when line breaks differ."""
    words: list[str] = []
    owners: list[int] = []
    for index, line in enumerate(lines):
        for word in line.split():
            words.append(word)
            owners.append(index)
    lower = [word.lower() for word in words]
    window = 10
    seen: dict[tuple[str, ...], int] = {}
    drop_lines: set[int] = set()
    for index in range(0, max(len(lower) - window + 1, 0)):
        key = tuple(lower[index : index + window])
        if key not in seen:
            seen[key] = index
            continue
        start = seen[key]
        size = 0
        while start + size < index and index + size < len(lower) and lower[start + size] == lower[index + size]:
            size += 1
        if size >= window:
            drop_lines.update(owners[index : index + size])
        break
    if not drop_lines:
        return lines
    return [line for index, line in enumerate(lines) if index not in drop_lines]


def _strip_tailor_suffix(text: str) -> str:
    cleaned = re.sub(r"\s*Core strengths for this role include [^.]*\.", "", text)
    cleaned = re.sub(r"\s*Prepared for [^.]*\.", "", cleaned)
    return cleaned.strip() + ("\n" if cleaned.strip() else "")


def resume_from_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Shape a stored user profile into the resume JSON the tailor expects."""
    source_text = str(profile.get("resume_document") or profile.get("resume_text") or "").strip()
    parsed = _parsed_resume_text(source_text)
    parsed_profile = (parsed or {}).get("profile") or {}
    name = parsed_profile.get("full_name") or _join_name(profile.get("first_name"), profile.get("last_name"))
    location = parsed_profile.get("location") or _join_place(profile.get("city"), profile.get("state"), profile.get("country"))
    history = _experience_from_history(profile.get("work_history") or [])
    history = [item for item in history if _looks_like_real_job(item)]
    parsed_jobs = list((parsed or {}).get("experience") or [])
    experience = _richer_experience(history, parsed_jobs)
    schools = _education_from_profile(profile.get("education") or [])
    if not schools and parsed:
        schools = list(parsed.get("education") or [])
    summary = (
        parsed_profile.get("summary")
        or _or_none(profile.get("professional_summary"))
        or None
    )
    if not summary:
        text = str(profile.get("professional_summary") or profile.get("resume_text") or "").strip()
        if text and (parsed is None or not parsed_profile.get("summary")):
            summary = text[:600]
    return {
        "profile": {
            "full_name": name,
            "email": parsed_profile.get("email") or _or_none(profile.get("email")),
            "phone": parsed_profile.get("phone") or _or_none(profile.get("phone")),
            "phones": [],
            "location": location,
            "linkedin": parsed_profile.get("linkedin") or _or_none(profile.get("linkedin_url")),
            "github": parsed_profile.get("github") or _or_none(profile.get("github_url")),
            "website": parsed_profile.get("website") or _or_none(profile.get("portfolio_url") or profile.get("personal_website")),
            "summary": summary,
        },
        "skills": _unique([*((parsed or {}).get("skills") or []), *_real_skills(profile.get("skills") or [])]),
        "experience": experience,
        "education": schools,
        "projects": list((parsed or {}).get("projects") or []),
        "certifications": _unique([*((parsed or {}).get("certifications") or []), *_real_skills(profile.get("certifications") or [])]),
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


def _looks_like_real_job(item: dict) -> bool:
    """Keep real work history. Drop bare job-board titles with no company, dates, or bullets."""
    if item.get("company") or item.get("start_date") or item.get("bullets"):
        return True
    title = str(item.get("title") or "").strip()
    return bool(title) and not _is_posting_title(title)


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


def _real_skills(values) -> list[str]:
    kept = []
    for value in values:
        text = str(value or "").strip()
        if not text or _is_junk_line(text):
            continue
        kept.append(text)
    return kept


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
