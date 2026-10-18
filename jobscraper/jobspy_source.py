"""Indeed, Glassdoor, ZipRecruiter, and Google Jobs via python-jobspy.

LinkedIn stays on the existing Jina path. JobSpy rate-limits LinkedIn quickly,
and Bayt/Naukri sit outside the US-only filter.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

log = logging.getLogger("jobscraper.jobspy")

SITE_TO_PLATFORM = {
    "indeed": "Indeed",
    "glassdoor": "Glassdoor",
    "zip_recruiter": "ZipRecruiter",
    "google": "Google Jobs",
}

PLATFORM_TO_SITE = {name: site for site, name in SITE_TO_PLATFORM.items()}


def sites_for_platform(platform: str) -> list[str]:
    selected = platform or "All"
    if selected == "All":
        return list(SITE_TO_PLATFORM)
    site = PLATFORM_TO_SITE.get(selected)
    return [site] if site else []


def _blank(value):
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    if isinstance(value, str) and value.strip().lower() in {"", "nan", "nat", "none"}:
        return None
    return value


def _posted(value) -> str:
    value = _blank(value)
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text[:10] if len(text) >= 10 else text


def _skills(value) -> list[str]:
    value = _blank(value)
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()][:8]
    return [part.strip() for part in str(value).split(",") if part.strip()][:8]


def rows_to_jobs(records: list[dict]) -> list[dict]:
    jobs = []
    for row in records:
        title = _blank(row.get("title"))
        url = _blank(row.get("job_url"))
        if not title or not url:
            continue
        site = str(_blank(row.get("site")) or "jobspy").lower()
        raw_id = _blank(row.get("id")) or url
        location = str(_blank(row.get("location")) or "").strip()
        remote = bool(row.get("is_remote"))
        if not location:
            location = "Remote" if remote else "United States"
        elif remote and "remote" not in location.lower():
            location = f"{location} · Remote"
        description = _blank(row.get("description")) or ""
        jobs.append(
            {
                "id": f"{site}-{raw_id}"[:255],
                "title": str(title).strip(),
                "company": str(_blank(row.get("company")) or "Unknown").strip() or "Unknown",
                "skills": _skills(row.get("skills")),
                "location": location,
                "platform": SITE_TO_PLATFORM.get(site, site.replace("_", " ").title()),
                "url": str(url).strip(),
                "posted_at": _posted(row.get("date_posted")),
                "job_type": _blank(row.get("job_type")),
                "search_text": str(description),
                "raw": row,
            }
        )
    return jobs


def scrape(
    search_terms: list[str],
    location: str,
    sites: list[str],
    *,
    is_remote: bool = False,
    results_wanted: int = 15,
    max_terms: int = 2,
) -> list[dict]:
    """Return job dicts. Empty list if JobSpy is missing or every site fails."""
    if not sites:
        return []
    terms = [term.strip() for term in search_terms if term and term.strip()][:max_terms]
    if not terms:
        return []
    try:
        from jobspy import scrape_jobs
    except ImportError:
        log.warning("python-jobspy is not installed; skipping Indeed/Glassdoor/ZipRecruiter/Google")
        return []

    found: list[dict] = []
    where = location or "United States"
    for term in terms:
        kwargs = {
            "site_name": sites,
            "search_term": term,
            "location": where,
            "results_wanted": results_wanted,
            "country_indeed": "USA",
            "verbose": 0,
            "description_format": "markdown",
        }
        if "google" in sites:
            kwargs["google_search_term"] = f"{term} jobs near {where}"
        # Indeed accepts only one of hours_old or is_remote.
        if is_remote:
            kwargs["is_remote"] = True
        else:
            kwargs["hours_old"] = 168
        try:
            frame = scrape_jobs(**kwargs)
        except Exception:
            log.exception("JobSpy failed for %r on %s", term, sites)
            continue
        if frame is None or getattr(frame, "empty", True):
            continue
        found.extend(rows_to_jobs(frame.to_dict("records")))
    return found
