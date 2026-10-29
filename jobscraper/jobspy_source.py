"""Indeed, LinkedIn, Glassdoor, Google Jobs, and Bayt via python-jobspy.

Searches are locked to Saudi Arabia (sa.indeed.com and Bayt).
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime

log = logging.getLogger("jobscraper.jobspy")
# One board call must not sit for hours. The crawl saves after every finished search.
_SEARCH_TIMEOUT_SECONDS = 150

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


def _posted(value, *, precise: bool = False) -> str:
    """Keep full timestamp when JobSpy gives one; relative labels become absolute.

    precise=True skips calendar-day-only values (midnight). LinkedIn search often
    only returns the date, which would wrongly show as '14 hours ago' at night.
    """
    from jobscraper.agent_reach import absolute_posted_at

    value = _blank(value)
    if value is None:
        return ""
    if isinstance(value, date) and not isinstance(value, datetime):
        if precise:
            return ""
        return f"{value.isoformat()} 00:00:00"
    return absolute_posted_at(value, allow_date_only=not precise)


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
                # LinkedIn/Indeed search cards are usually date-only. Keep that in
                # posted_at for coarse filters, but only treat a real clock time as
                # platform_posted_at so "6 hours ago" can be fetched from the page.
                "platform_posted_at": _posted(row.get("date_posted"), precise=True),
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
                frame = _scrape_jobs_bounded(scrape_jobs, kwargs)
            except Exception:
                log.exception("JobSpy failed for %r on %s", term, site)
                continue
            if frame is None or getattr(frame, "empty", True):
                continue
            found.extend(rows_to_jobs(frame.to_dict("records")))
    return _fill_short_descriptions(found)


def _scrape_jobs_bounded(scrape_jobs, kwargs: dict):
    """Return the JobSpy frame, or None when this one board search does not finish."""
    box: dict = {}

    def run() -> None:
        try:
            box["frame"] = scrape_jobs(**kwargs)
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(_SEARCH_TIMEOUT_SECONDS)
    if thread.is_alive():
        log.warning(
            "JobSpy timed out after %ss for %r on %s",
            _SEARCH_TIMEOUT_SECONDS,
            kwargs.get("search_term"),
            kwargs.get("site_name"),
        )
        return None
    if "error" in box:
        raise box["error"]
    return box.get("frame")


def _fill_short_descriptions(jobs: list[dict]) -> list[dict]:
    """LinkedIn and Bayt search cards omit the posting. Load the full page text."""
    import time
    import urllib.error

    from jobscraper.agent_reach import fetch_posting_details, is_date_only_stamp

    filled = 0
    for job in jobs:
        current = str(job.get("search_text") or "").strip()
        title = str(job.get("title") or "").strip()
        platform = job.get("platform") or ""
        if platform not in {"LinkedIn", "Bayt", "Indeed"}:
            continue
        needs_text = len(current) < 400 or current.casefold() == title.casefold()
        existing_platform = str(job.get("platform_posted_at") or "").strip()
        # LinkedIn guest pages carry "6 hours ago". Date-only JobSpy stamps must not
        # block that fetch, or daysAgo stays stuck on midnight ("14 hours ago").
        needs_posted = platform == "LinkedIn" and (
            not existing_platform or is_date_only_stamp(existing_platform)
        )
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
        if platform_posted and needs_posted and not is_date_only_stamp(platform_posted):
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
