"""Registration profiles keyed by the caller's candidate API id."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from jobscraper.locations import matches_city_filter, matches_state_filter, region_name
from jobscraper.recommend import normalize_skills
from jobscraper.resume_parser import SKILL_CANONICAL, SKILL_PATTERN

_ROOT = Path(__file__).resolve().parent.parent
CANDIDATES_FILE = _ROOT / "data" / "candidates.json"
_FILE_LOCK = threading.Lock()
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_PRESENT_RE = re.compile(r"present|current", re.I)
_YES = {"yes", "y", "true", "1"}
_NO = {"no", "n", "false", "0"}


class CandidateError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def clean_candidate_id(value: str) -> str:
    text = str(value or "").strip()
    if not _ID_RE.fullmatch(text):
        raise CandidateError("candidate_id must be 1-64 letters, numbers, underscore, or hyphen.")
    return text


def yn(value) -> str:
    text = str(value or "").strip().lower()
    if text in _YES:
        return "yes"
    if text in _NO:
        return "no"
    return ""


def canonical_state(value: str) -> str:
    return region_name(value)


def _clip(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _items(raw, keys: tuple[str, ...], limit: int) -> list[dict]:
    rows = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        rows.append({key: _clip(item.get(key), 4000) for key in keys})
        if len(rows) >= limit:
            break
    return rows


def normalize_profile(raw: dict | None) -> dict:
    """Keep the registration fields used for autofill and matching.

    Password and EEO answers are dropped. They are not used to rank jobs.
    """
    source = raw or {}
    return {
        "first_name": _clip(source.get("first_name"), 80),
        "last_name": _clip(source.get("last_name"), 80),
        "preferred_name": _clip(source.get("preferred_name"), 80),
        "pronouns": _clip(source.get("pronouns"), 40),
        "email": _clip(source.get("email"), 160),
        "phone_code": _clip(source.get("phone_code"), 8),
        "phone": _clip(source.get("phone"), 32),
        "city": _clip(source.get("city"), 80),
        "state": canonical_state(source.get("state") or "") or _clip(source.get("state"), 80),
        "zip_code": _clip(source.get("zip_code"), 16),
        "country": _clip(source.get("country"), 80),
        "linkedin_url": _clip(source.get("linkedin_url"), 300),
        "github_url": _clip(source.get("github_url"), 300),
        "portfolio_url": _clip(source.get("portfolio_url"), 300),
        "personal_website": _clip(source.get("personal_website"), 300),
        "work_history": _items(
            source.get("work_history"),
            ("company", "job_title", "start_date", "end_date", "description"),
            20,
        ),
        "education": _items(
            source.get("education"),
            ("school", "degree", "field_of_study", "graduation_year", "gpa"),
            10,
        ),
        "custom_answers": _items(source.get("custom_answers"), ("question", "answer"), 30),
        "skills": normalize_skills(source.get("skills") or []),
        "resume_text": _clip(source.get("resume_text"), 200_000),
        "cover_letter_text": _clip(source.get("cover_letter_text"), 50_000),
        "authorized_to_work": yn(source.get("authorized_to_work")),
        "need_visa_sponsorship": yn(source.get("need_visa_sponsorship")),
        "willing_to_relocate": yn(source.get("willing_to_relocate")),
        "open_to_remote": yn(source.get("open_to_remote")),
        "is_18_or_older": yn(source.get("is_18_or_older")),
        "desired_salary": _clip(source.get("desired_salary"), 40),
        "notice_period": _clip(source.get("notice_period"), 40),
        "how_heard": _clip(source.get("how_heard"), 80),
    }


def skills_from_profile(profile: dict) -> list[str]:
    chunks = [
        profile.get("resume_text") or "",
        profile.get("cover_letter_text") or "",
    ]
    for job in profile.get("work_history") or []:
        chunks.append(job.get("job_title") or "")
        chunks.append(job.get("description") or "")
    for school in profile.get("education") or []:
        chunks.append(school.get("degree") or "")
        chunks.append(school.get("field_of_study") or "")
    for answer in profile.get("custom_answers") or []:
        chunks.append(answer.get("answer") or "")
    found = list(profile.get("skills") or [])
    blob = "\n".join(chunks)
    if blob.strip():
        for match in SKILL_PATTERN.findall(blob):
            found.append(SKILL_CANONICAL[match.lower()])
    return normalize_skills(found)


def _year(value: str) -> int | None:
    match = _YEAR_RE.search(str(value or ""))
    return int(match.group(0)) if match else None


def career_years(profile: dict) -> int | None:
    starts: list[int] = []
    ends: list[int] = []
    this_year = datetime.now(timezone.utc).year
    for job in profile.get("work_history") or []:
        start = _year(job.get("start_date") or "")
        if start is None:
            continue
        end_text = job.get("end_date") or ""
        end = this_year if _PRESENT_RE.search(end_text) else (_year(end_text) or this_year)
        starts.append(start)
        ends.append(end)
    if not starts:
        return None
    return max(0, max(ends) - min(starts))


def _job_is_remote(job: dict) -> bool:
    if (job.get("work_model") or "") == "remote":
        return True
    if (job.get("state") or "") == "Remote":
        return True
    return "remote" in str(job.get("location") or "").lower()


def applied_filters(profile: dict, *, state: str, city: str) -> dict:
    override = canonical_state(state) if state and state != "All" else ""
    home = canonical_state(profile.get("state") or "")
    relocate = yn(profile.get("willing_to_relocate"))
    target = override or ("" if relocate == "yes" else home)
    return {
        "state": target or "All",
        "city": (city or "").strip(),
        "open_to_remote": yn(profile.get("open_to_remote")),
        "willing_to_relocate": relocate,
        "need_visa_sponsorship": yn(profile.get("need_visa_sponsorship")),
        "authorized_to_work": yn(profile.get("authorized_to_work")),
        "years": career_years(profile),
    }


def filter_jobs_for_candidate(jobs: list[dict], profile: dict, *, state: str = "All", city: str = "") -> list[dict]:
    """Apply registration preferences: location, remote, visa, and years."""
    filters = applied_filters(profile, state=state, city=city)
    target_state = "" if filters["state"] == "All" else filters["state"]
    state_was_explicit = bool(canonical_state(state) if state and state != "All" else "")
    selected_city = filters["city"]
    remote_choice = filters["open_to_remote"]
    needs_sponsor = filters["need_visa_sponsorship"] == "yes" or filters["authorized_to_work"] == "no"
    years = filters["years"]
    kept = []
    for job in jobs:
        if needs_sponsor:
            if (job.get("visa_sponsorship") or "unknown") == "no":
                continue
            if (job.get("saudi_national_required") or "no") == "yes":
                continue
        if years is not None:
            required = job.get("years_experience")
            if required is not None and int(required) > int(years):
                continue
        if remote_choice == "no" and _job_is_remote(job):
            continue
        remote = _job_is_remote(job)
        if target_state:
            in_state = matches_state_filter(job.get("state") or "", job.get("location") or "", target_state)
            allow_remote = remote and remote_choice != "no" and not state_was_explicit
            if not in_state and not allow_remote:
                continue
            if selected_city and in_state and not remote and not matches_city_filter(job.get("location") or "", selected_city):
                continue
        elif selected_city and not remote and not matches_city_filter(job.get("location") or "", selected_city):
            continue
        kept.append(job)
    return kept


def _read_file() -> dict:
    if not CANDIDATES_FILE.exists():
        return {}
    try:
        payload = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_file(payload: dict) -> None:
    CANDIDATES_FILE.parent.mkdir(exist_ok=True)
    CANDIDATES_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def save_candidate_file(candidate_id: str, profile: dict) -> None:
    with _FILE_LOCK:
        payload = _read_file()
        payload[candidate_id] = profile
        _write_file(payload)


def load_candidate_file(candidate_id: str) -> dict | None:
    with _FILE_LOCK:
        profile = _read_file().get(candidate_id)
    return profile if isinstance(profile, dict) else None
