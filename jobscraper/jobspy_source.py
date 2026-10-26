"""Indeed, LinkedIn, Glassdoor, Google Jobs, and Bayt via python-jobspy.

Searches are locked to Saudi Arabia (sa.indeed.com and Bayt).
"""

from __future__ import annotations

import logging
from datetime import date, datetime

log = logging.getLogger("jobscraper.jobspy")

SITE_TO_PLATFORM = {
    "indeed": "Indeed",
    "linkedin": "LinkedIn",
    "google": "Google Jobs",
    "bayt": "Bayt",
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
            location = "Remote, Saudi Arabia" if remote else "Saudi Arabia"
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
    results_wanted: int = 20,
    max_terms: int = 4,
    hours_old: int = 720,
) -> list[dict]:
    """Return Saudi job dicts. Empty list if JobSpy is missing or every site fails."""
    if not sites:
        return []
    terms = [term.strip() for term in search_terms if term and term.strip()][:max_terms]
    if not terms:
        return []
    try:
        from jobspy import scrape_jobs
    except ImportError:
        log.warning("python-jobspy is not installed; skipping Indeed/LinkedIn/Glassdoor/Google/Bayt")
        return []

    found: list[dict] = []
    where = location or "Saudi Arabia"
    # One site per call. JobSpy cancels the whole search when one board throws,
    # and Glassdoor has no Saudi Arabia domain.
    for site in sites:
        for term in terms:
            kwargs = {
                "site_name": [site],
                "search_term": term,
                "location": where,
                "results_wanted": results_wanted,
                "country_indeed": "saudi arabia",
                "verbose": 0,
                "description_format": "markdown",
            }
            if site == "google":
                kwargs["google_search_term"] = f"{term} jobs in {where}"
            if is_remote:
                kwargs["is_remote"] = True
            else:
                kwargs["hours_old"] = hours_old
            try:
                frame = scrape_jobs(**kwargs)
            except Exception:
                log.exception("JobSpy failed for %r on %s", term, site)
                continue
            if frame is None or getattr(frame, "empty", True):
                continue
            found.extend(rows_to_jobs(frame.to_dict("records")))
    return _fill_short_descriptions(found)


def _fill_short_descriptions(jobs: list[dict]) -> list[dict]:
    """LinkedIn and Bayt search cards omit the posting. Load the full page text."""
    import time

    from jobscraper.agent_reach import fetch_posting_paragraph

    filled = 0
    for job in jobs:
        current = str(job.get("search_text") or "").strip()
        if len(current) >= 400 or job.get("platform") not in {"LinkedIn", "Bayt", "Indeed"}:
            continue
        try:
            full = fetch_posting_paragraph(job.get("platform") or "", job.get("url") or "")
        except Exception:
            log.exception("Description fetch failed for %s", job.get("url"))
            continue
        time.sleep(0.15)
        if full and len(full) > len(current):
            job["search_text"] = full
            filled += 1
    if filled:
        log.info("Filled %s full descriptions from posting pages", filled)
    return jobs
