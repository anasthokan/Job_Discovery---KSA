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
    """Keep full timestamp when JobSpy gives one; relative labels become absolute."""
    from jobscraper.agent_reach import absolute_posted_at

    value = _blank(value)
    if value is None:
        return ""
    if isinstance(value, datetime):
        return absolute_posted_at(value)
    if isinstance(value, date):
        return f"{value.isoformat()} 00:00:00"
    return absolute_posted_at(value)


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
                "platform_posted_at": _posted(row.get("date_posted")),
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
    import urllib.error

    from jobscraper.agent_reach import fetch_posting_details

    filled = 0
    for job in jobs:
        current = str(job.get("search_text") or "").strip()
        title = str(job.get("title") or "").strip()
        platform = job.get("platform") or ""
        if platform not in {"LinkedIn", "Bayt", "Indeed"}:
            continue
        needs_text = len(current) < 400 or current.casefold() == title.casefold()
        needs_posted = not str(job.get("platform_posted_at") or job.get("posted_at") or "").strip()
        if not needs_text and not needs_posted:
            continue
        details = {"description": "", "platform_posted_at": ""}
        for attempt in range(4):
            try:
                details = fetch_posting_details(platform, job.get("url") or "")
                break
            except urllib.error.HTTPError as exc:
                if exc.code in {404, 410}:
                    break
                if exc.code == 429 and attempt < 3:
                    time.sleep(12)
                    continue
                log.warning("Description fetch failed for %s", job.get("url"))
                break
            except Exception:
                log.exception("Description fetch failed for %s", job.get("url"))
                break
        time.sleep(0.8)
        full = str(details.get("description") or "")
        platform_posted = str(details.get("platform_posted_at") or "").strip()
        if platform_posted and needs_posted:
            job["platform_posted_at"] = platform_posted
            job["posted_at"] = platform_posted
        if needs_text and full and len(full) > len(current):
            job["search_text"] = full
            filled += 1
        elif needs_text and title and current.casefold() == title.casefold():
            job["search_text"] = ""
    if filled:
        log.info("Filled %s full descriptions from posting pages", filled)
    return jobs
