"""Rank scraped job listings against skills, certifications, and experience."""

from __future__ import annotations

import re

from jobscraper.jd_to_cv import SKILL_RELATED
from jobscraper.resume_parser import SKILL_CANONICAL

DEFAULT_LIMIT = 25
MAX_LIMIT = 100

_TITLE_NOISE = {
    "a",
    "an",
    "and",
    "at",
    "for",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
    "i",
    "ii",
    "iii",
    "iv",
}
# Seniority and structure words. "Senior" plus "Manager" is not a title match.
_TITLE_GENERIC = _TITLE_NOISE | {
    "acting",
    "assistant",
    "associate",
    "chief",
    "coordinator",
    "director",
    "executive",
    "global",
    "group",
    "head",
    "interim",
    "intern",
    "internship",
    "jr",
    "junior",
    "lead",
    "leader",
    "leading",
    "level",
    "management",
    "manager",
    "mid",
    "officer",
    "president",
    "principal",
    "regional",
    "senior",
    "specialist",
    "sr",
    "staff",
    "team",
    "vice",
    "vp",
}
_NOT_A_SKILL = {
    "advanced",
    "basic",
    "beginner",
    "expert",
    "fluent",
    "intermediate",
    "native",
    "professional",
}
_HEX_ID = re.compile(r"^[0-9a-f]{16,}$", re.I)
_LEVEL_RANK = {
    "intern/new grad": 0,
    "entry level": 1,
    "mid level": 2,
    "senior level": 3,
    "lead/staff": 4,
    "director/executive": 5,
}
_DIRECTOR = re.compile(r"\b(director|vice president|\bvp\b|head of|chief)\b", re.I)
_LEAD = re.compile(r"\b(lead|principal|staff)\b", re.I)
_SENIOR = re.compile(r"\b(senior|sr\.?)\b", re.I)


def normalize_skills(raw: list[str] | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in raw or []:
        skill = str(value or "").strip(" ,;|/").strip()
        if not skill:
            continue
        skill = SKILL_CANONICAL.get(skill.lower(), skill)
        key = skill.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(skill)
    return out


def parse_skill_query(raw: str) -> list[str]:
    parts = [part.strip() for part in (raw or "").replace(";", ",").split(",")]
    return normalize_skills(parts)


def recommend_jobs(
    jobs: list[dict],
    skills: list[str],
    *,
    certifications: list[str] | None = None,
    experience_titles: list[str] | None = None,
    years: int | None = None,
    limit: int | None = DEFAULT_LIMIT,
) -> list[dict]:
    """Rank every positive match. `limit=None` keeps the full set; an int keeps the top N (max 100)."""
    wanted = _usable_terms(skills)
    certs = _usable_terms(certifications)
    titles = _clean_titles(experience_titles)
    if not wanted and not certs and not titles:
        return []
    ranked = []
    for job in jobs:
        match = score_job(
            job,
            wanted,
            certifications=certs,
            experience_titles=titles,
            years=years,
        )
        if match["score"] <= 0:
            continue
        ranked.append({**job, "match": match})
    ranked.sort(
        key=lambda row: (
            -row["match"]["score"],
            -(1 if row["match"].get("experience_fit") == "match" else 0),
            row.get("daysAgo") or 0,
            row.get("title") or "",
        )
    )
    if limit is None:
        return ranked
    return ranked[:_clamp_limit(limit)]


def score_job(
    job: dict,
    skills: list[str],
    *,
    certifications: list[str] | None = None,
    experience_titles: list[str] | None = None,
    years: int | None = None,
) -> dict:
    wanted = _usable_terms(skills)
    certs = _usable_terms(certifications)
    titles = _clean_titles(experience_titles)
    job_skills = normalize_skills(job.get("skills") or [])
    description = str(job.get("description") or "")
    title = str(job.get("title") or "")
    blob = " ".join(
        [
            title,
            str(job.get("company") or ""),
            str(job.get("location") or ""),
            str(job.get("state") or ""),
            " ".join(job_skills),
            description,
        ]
    ).lower()
    title_l = title.lower()
    job_keys = {item.lower() for item in job_skills}

    matched, missing, skill_points = _overlap(wanted, job_keys, title_l, blob, hit=2, soft=1)
    matched_certs, missing_certs, cert_points = _overlap(certs, job_keys, title_l, blob, hit=3, soft=2)
    matched_titles = [item for item in titles if _title_overlap(item, title)]
    year_points, experience_fit = _experience_points(job, titles, years)

    # Years alone are not a match. A seniority-word title overlap is not one either.
    if not matched and not matched_certs and not matched_titles:
        skill_points = cert_points = year_points = 0
        experience_fit = "none"
    score = _blend_score(
        skill_points=skill_points,
        skill_max=len(wanted) * 2,
        cert_points=cert_points,
        cert_max=len(certs) * 3,
        title_hit=bool(matched_titles),
        has_titles=bool(titles),
        year_points=year_points if matched_titles or skill_points >= 4 or cert_points else 0,
    )
    return {
        "score": score,
        "matched_skills": matched,
        "missing_skills": missing,
        "matched_certifications": matched_certs,
        "missing_certifications": missing_certs,
        "matched_titles": matched_titles,
        "experience_fit": experience_fit if score else "none",
    }


def _usable_terms(raw: list[str] | None) -> list[str]:
    """Drop stored ids and proficiency labels so they cannot flatten every score."""
    out: list[str] = []
    for skill in normalize_skills(raw):
        key = skill.lower().replace("-", "")
        if key in _NOT_A_SKILL or _HEX_ID.match(key):
            continue
        out.append(skill)
    return out


def _blend_score(
    *,
    skill_points: int,
    skill_max: int,
    cert_points: int,
    cert_max: int,
    title_hit: bool,
    has_titles: bool,
    year_points: int,
) -> int:
    parts: list[tuple[float, float]] = []
    if skill_max:
        parts.append((skill_points / skill_max, 0.55))
    if cert_max:
        parts.append((cert_points / cert_max, 0.15))
    if has_titles:
        parts.append((1.0 if title_hit else 0.0, 0.20))
    if year_points:
        parts.append((min(year_points, 5) / 5, 0.10))
    if not parts:
        return 0
    weight = sum(item[1] for item in parts)
    covered = sum(item[0] * item[1] for item in parts)
    if covered <= 0:
        return 0
    return min(100, int(round(100 * covered / weight)))


def _overlap(wanted: list[str], job_keys: set[str], title: str, blob: str, *, hit: int, soft: int):
    matched: list[str] = []
    missing: list[str] = []
    points = 0
    for skill in wanted:
        key = skill.lower()
        related = SKILL_RELATED.get(key, set())
        in_listed = key in job_keys or any(item in job_keys for item in related)
        in_title = key in title or any(item in title for item in related)
        in_blob = key in blob or any(item in blob for item in related)
        if in_listed or in_title or in_blob:
            matched.append(skill)
            points += hit if in_listed or in_title else soft
        else:
            missing.append(skill)
    return matched, missing, points


def _clean_titles(raw: list[str] | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in raw or []:
        title = re.sub(r"\s+", " ", str(value or "")).strip(" ,;|-")
        key = title.lower()
        if len(title) < 2 or key in seen:
            continue
        seen.add(key)
        out.append(title)
    return out


def _tokens(value: str) -> set[str]:
    return {part for part in re.findall(r"[a-z0-9+#]+", value.lower()) if part not in _TITLE_NOISE and len(part) > 1}


def _title_overlap(candidate_title: str, job_title: str) -> bool:
    left = _tokens(candidate_title) - _TITLE_GENERIC
    right = _tokens(job_title) - _TITLE_GENERIC
    if not left or not right:
        return False
    if left <= right or right <= left:
        return True
    return len(left & right) >= 2


def _candidate_level(titles: list[str], years: int | None) -> int | None:
    blob = " ".join(titles)
    if _DIRECTOR.search(blob):
        return 5
    if _LEAD.search(blob):
        return 4
    if _SENIOR.search(blob) or (years or 0) >= 6:
        return 3
    if years is None:
        return None
    if years >= 3:
        return 2
    if years >= 1:
        return 1
    return 0


def _experience_points(job: dict, titles: list[str], years: int | None) -> tuple[int, str]:
    points = 0
    fit = "unknown"
    required = job.get("years_experience")
    if years is not None and required is not None:
        gap = int(years) - int(required)
        if gap >= 0:
            points += 3 if gap <= 3 else 2
            fit = "match"
        else:
            fit = "stretch"
    candidate_level = _candidate_level(titles, years)
    job_level = _LEVEL_RANK.get(str(job.get("experience_level") or "").lower())
    if candidate_level is not None and job_level is not None and abs(candidate_level - job_level) <= 1:
        points += 2
        if fit == "unknown":
            fit = "match"
    return points, fit


def _clamp_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))
