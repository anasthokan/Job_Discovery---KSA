"""Open jobs from a company's own career portal.

The company name is resolved to a public careers page. Known applicant-tracking
feeds (Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee,
BambooHR, Personio, Workday) are read as JSON. Other portals are read from the
job links and JobPosting markup on the careers page.
"""

from __future__ import annotations

import html
import ipaddress
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed, wait
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

from jobscraper.job_fields import infer_listing_fields

log = logging.getLogger("jobscraper.career")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
FETCH_CAP = 1000
POSITIVE_TTL = 900
NEGATIVE_TTL = 300
SKIP_LABELS = {
    "linkedin",
    "indeed",
    "glassdoor",
    "bayt",
    "naukri",
    "naukrigulf",
    "gulftalent",
    "gulfcareers",
    "gccrecruitments",
    "oilgasalert",
    "rigzone",
    "ziprecruiter",
    "monster",
    "facebook",
    "instagram",
    "youtube",
    "wikipedia",
    "twitter",
    "reddit",
    "google",
    "bing",
}
LEGAL = {
    "inc",
    "llc",
    "ltd",
    "limited",
    "corp",
    "corporation",
    "company",
    "co",
    "group",
    "holding",
    "holdings",
    "plc",
    "the",
    "and",
    "of",
}
GENERIC_SLUG = {
    "national",
    "saudi",
    "bank",
    "group",
    "energy",
    "global",
    "digital",
    "services",
    "solutions",
    "international",
    "industries",
    "industrial",
    "arabian",
    "arabia",
    "holding",
    "company",
}
NAV_TITLES = {
    "apply",
    "apply now",
    "learn more",
    "read more",
    "view",
    "view job",
    "see details",
    "details",
    "careers",
    "jobs",
    "job",
    "search",
    "find a job",
    "submit",
    "home",
}
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()
_JSON_LOCK = threading.Lock()
_JSON_MEMO: dict[str, tuple[float, object]] = {}
_JSON_INFLIGHT: dict[str, threading.Event] = {}
_JOB_PATH = re.compile(
    r"/(?:jobs?|openings?|positions?|vacanc(?:y|ies))/[^/]+",
    re.I,
)
_ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
_JSONLD = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
_RESULT_LINK = re.compile(
    r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.I | re.S,
)


def lookup_company_jobs(
    company: str,
    *,
    location: str = "",
    limit: int = 50,
    refresh: bool = False,
) -> dict:
    """Return open jobs listed on the company's public career portal."""
    company = re.sub(r"\s+", " ", (company or "").strip())
    if len(company) < 2 or len(company) > 120:
        raise ValueError("Enter a company name.")
    limit = max(1, min(int(limit or 50), 100))
    location = (location or "").strip()[:80]
    key = company.casefold()
    if not refresh:
        cached = _cache_get(key)
        if cached is not None:
            return _present(company, cached, location, limit)

    hit, finished = _find_hit(company)
    if hit and hit.get("jobs"):
        _cache_put(key, hit, POSITIVE_TTL)
    elif finished:
        _cache_put(key, hit or {}, 120)
    return _present(company, hit, location, limit)


def _present(company: str, hit: dict | None, location: str, limit: int) -> dict:
    hit = hit or {}
    if not hit.get("career_page"):
        return {
            "ok": True,
            "company": company,
            "portal_company": None,
            "found": False,
            "portal": None,
            "career_page": None,
            "count": 0,
            "total": 0,
            "limit": limit,
            "jobs": [],
            "message": "No public career portal was found for this company. Try the name used on their careers site.",
        }
    loaded = list(hit.get("jobs") or [])
    portal_total = int(hit.get("total") or len(loaded))
    jobs = loaded
    message = None
    if location:
        needle = location.casefold()
        jobs = [job for job in loaded if needle in str(job.get("location") or "").casefold()]
        total = len(jobs)
        if not jobs and portal_total:
            message = "The career portal has open jobs, but none matched this location."
        elif not jobs:
            message = "The career page was found, but no open jobs were listed."
    else:
        total = portal_total
        if not jobs:
            message = "The career page was found, but no open jobs were listed."
        elif portal_total > len(loaded):
            message = f"This portal lists {portal_total} open jobs."
    page = jobs[:limit]
    official = str(hit.get("official_name") or "").strip() or company
    return {
        "ok": True,
        "company": company,
        "portal_company": official,
        "found": True,
        "portal": hit.get("portal") or "Career page",
        "career_page": hit.get("career_page"),
        "count": len(page),
        "total": total,
        "limit": limit,
        "jobs": page,
        "message": message,
    }


def _find_hit(company: str) -> tuple[dict | None, bool]:
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="career")
    search_future = pool.submit(_search_hit, company)
    slug_future = pool.submit(_slug_hit, company)
    done, _pending = wait([search_future, slug_future], timeout=36)
    hits = []
    for future in done:
        try:
            hit = future.result()
        except Exception:
            log.exception("Career lookup failed for %s", company)
            hit = None
        if hit and hit.get("career_page"):
            hits.append(hit)
    _shutdown(pool)
    finished = search_future in done and slug_future in done
    if not hits:
        return None, finished
    hits.sort(key=lambda item: _score(item, company), reverse=True)
    chosen = hits[0]
    log.info(
        "Career portal %s for %s via %s (%s jobs)",
        chosen.get("career_page"),
        company,
        chosen.get("portal"),
        len(chosen.get("jobs") or []),
    )
    return chosen, finished


def _score(hit: dict, company: str) -> int:
    jobs = hit.get("jobs") or []
    score = min(len(jobs), 40)
    if jobs:
        score += 100
    official = str(hit.get("official_name") or "")
    if official and _names_match(company, official):
        score += 40
    if _url_matches(str(hit.get("career_page") or ""), company):
        score += 15
    return score


def _search_hit(company: str) -> dict | None:
    links = _search_links(company)
    if not links:
        links = _guess_urls(company)
    if not links:
        return None
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="career-page")
    futures = [pool.submit(_resolve_link, url, company) for url in links[:8]]
    hits: list[dict] = []
    fallback: dict | None = None
    try:
        for future in as_completed(futures, timeout=18):
            try:
                hit = future.result()
            except Exception:
                log.info("Career page read failed", exc_info=True)
                continue
            if not hit:
                continue
            if hit.get("jobs"):
                hits.append(hit)
            elif hit.get("career_page"):
                fallback = fallback or hit
    except TimeoutError:
        log.info("Career page search timed out for %s", company)
    _shutdown(pool)
    if hits:
        hits.sort(key=lambda item: _score(item, company), reverse=True)
        return hits[0]
    return fallback


def _resolve_link(url: str, company: str) -> dict | None:
    detected = _ats_from_url(url)
    if detected:
        hit = _load_ats(detected, company, require_name=False)
        if hit and (hit.get("jobs") or hit.get("official_name")):
            if hit.get("official_name") and not _names_match(company, hit["official_name"]):
                return None
            return hit
    page = _fetch(url, timeout=8, accept="text/html")
    if not page or page[0] >= 400 or not page[2]:
        return None
    final_url = page[1] or url
    hit = _hit_from_html(page[2], final_url, company)
    if hit and hit.get("jobs"):
        richer = _richer_listing(final_url, company, hit)
        return richer or hit
    root = _site_root(final_url)
    if root and root.rstrip("/") != final_url.rstrip("/"):
        home = _fetch(root, timeout=8, accept="text/html")
        if home and home[0] < 400 and home[2]:
            home_hit = _hit_from_html(home[2], home[1] or root, company)
            if home_hit and home_hit.get("jobs"):
                return home_hit
    if _careerish(final_url):
        return {
            "portal": "Career page",
            "career_page": final_url,
            "official_name": "",
            "jobs": [],
            "total": 0,
        }
    return None


def _richer_listing(page_url: str, company: str, current: dict) -> dict | None:
    """A site root often teases a few jobs and lists the rest on /hiring."""
    root = _site_root(page_url)
    if not root:
        return None
    hiring = urljoin(root, "hiring")
    if hiring.rstrip("/") == page_url.rstrip("/"):
        return None
    page = _fetch(hiring, timeout=8, accept="text/html")
    if not page or page[0] >= 400 or not page[2]:
        return None
    jobs = _jobs_from_html(page[2], page[1] or hiring, company)
    if len(jobs) <= len(current.get("jobs") or []):
        return None
    return {
        "portal": "Career page",
        "career_page": page[1] or hiring,
        "official_name": current.get("official_name") or "",
        "jobs": jobs,
        "total": len(jobs),
    }


def _guess_urls(company: str) -> list[str]:
    parts = [re.sub(r"[^a-z0-9]", "", part) for part in _tokens(company)]
    parts = [part for part in parts if part and part not in GENERIC_SLUG and len(part) >= 4]
    if not parts:
        return []
    token = max(parts, key=len)
    return [
        f"https://www.{token}.jobs/",
        f"https://{token}.jobs/",
        f"https://careers.{token}.com/",
        f"https://www.{token}.com/careers",
        f"https://www.{token}.com/en/careers",
    ]


def _hit_from_html(page: str, page_url: str, company: str) -> dict | None:
    detected = _ats_from_html(page)
    if detected:
        hit = _load_ats(detected, company, require_name=False)
        if hit and hit.get("jobs"):
            if hit.get("official_name") and not _names_match(company, hit["official_name"]):
                pass
            else:
                return hit
    jobs = _jobs_from_html(page, page_url, company)
    if not jobs:
        return None
    return {
        "portal": "Career page",
        "career_page": page_url,
        "official_name": "",
        "jobs": jobs,
        "total": len(jobs),
    }


def _jobs_from_html(page: str, page_url: str, company: str) -> list[dict]:
    """JobPosting markup wins when the same URL is also a plain link."""
    jobs = []
    seen = set()
    for job in _jobs_from_jsonld(page, page_url, company) + _jobs_from_links(page, page_url, company):
        key = job["url"].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        jobs.append(job)
        if len(jobs) >= FETCH_CAP:
            break
    return jobs


def _jobs_from_jsonld(page: str, page_url: str, company: str) -> list[dict]:
    jobs = []
    seen = set()
    for block in _JSONLD.findall(page or ""):
        try:
            payload = json.loads(html.unescape(block).strip())
        except json.JSONDecodeError:
            continue
        for node in _walk(payload):
            if not isinstance(node, dict) or not _is_jobposting(node):
                continue
            title = _plain(node.get("title") or "", 180)
            url = str(node.get("url") or page_url).strip()
            if url.startswith("/"):
                url = urljoin(page_url, url)
            url = _clean_url(url)
            if not title or not url or url in seen or _skipped(url):
                continue
            seen.add(url)
            org = node.get("hiringOrganization") or {}
            name = company
            if isinstance(org, dict) and org.get("name"):
                name = _plain(org.get("name"), 120) or company
            job = _make_job(
                source="Career page",
                job_id=url.rstrip("/").split("/")[-1][:80] or str(len(jobs) + 1),
                title=title,
                company=name,
                location=_jsonld_location(node),
                url=url,
                department=_plain(node.get("occupationalCategory") or node.get("industry") or "", 120),
                job_type=_employment(node.get("employmentType")),
                posted_at=str(node.get("datePosted") or ""),
                description=str(node.get("description") or ""),
            )
            if job:
                jobs.append(job)
            if len(jobs) >= FETCH_CAP:
                return jobs
    return jobs


def _jobs_from_links(page: str, page_url: str, company: str) -> list[dict]:
    jobs = []
    seen = set()
    for attrs, inner in _ANCHOR.findall(page or "")[:500]:
        href_match = _HREF.search(attrs or "")
        if not href_match:
            continue
        url = _clean_url(urljoin(page_url, html.unescape(href_match.group(1))))
        if not url or url in seen or not _is_job_url(url) or _skipped(url):
            continue
        if not _same_family(url, page_url, company):
            continue
        seen.add(url)
        title = _plain(inner, 180)
        slug_title = _title_from_url(url)
        if not _good_title(title):
            title = slug_title
        if not title:
            continue
        job = _make_job(
            source="Career page",
            job_id=url.rstrip("/").split("/")[-1][:80],
            title=title,
            company=company,
            location="",
            url=url,
        )
        if job:
            jobs.append(job)
        if len(jobs) >= FETCH_CAP:
            break
    return jobs


def _slug_hit(company: str) -> dict | None:
    names = _slugs(company)
    if not names:
        return None
    extra = _extra_slug(company)
    tasks = []
    for slug in names:
        single = slug == extra
        tasks.append(("greenhouse", slug, True))
        tasks.append(("smartrecruiters", slug, True))
        tasks.append(("workable", slug, True))
        if not single:
            tasks.append(("lever", slug, False))
            tasks.append(("ashby", slug, False))
            tasks.append(("recruitee", slug, False))
            tasks.append(("bamboohr", slug, False))
            tasks.append(("personio", slug, False))
    pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="career-ats")
    futures = [
        pool.submit(_load_ats, {"provider": provider, "token": slug}, company, require_name=require)
        for provider, slug, require in tasks
    ]
    hits = []
    try:
        for future in as_completed(futures, timeout=34):
            try:
                hit = future.result()
            except Exception:
                log.info("ATS probe failed", exc_info=True)
                continue
            if hit and hit.get("career_page"):
                if hit.get("official_name") and not _names_match(company, hit["official_name"]):
                    continue
                hits.append(hit)
    except TimeoutError:
        log.info("ATS probe timed out for %s", company)
    _shutdown(pool)
    if not hits:
        return None
    hits.sort(key=lambda item: _score(item, company), reverse=True)
    return hits[0]


def _load_ats(info: dict, company: str, *, require_name: bool) -> dict | None:
    provider = info.get("provider") or ""
    if provider == "workday":
        return _load_workday(info, company)
    token = str(info.get("token") or "").strip(" /")
    if not token or token.lower() in {"jobs", "job", "embed", "search", "api"}:
        return None
    loaders = {
        "greenhouse": _load_greenhouse,
        "lever": _load_lever,
        "ashby": _load_ashby,
        "smartrecruiters": _load_smartrecruiters,
        "workable": _load_workable,
        "recruitee": _load_recruitee,
        "bamboohr": _load_bamboohr,
        "personio": _load_personio,
    }
    loader = loaders.get(provider)
    if not loader:
        return None
    try:
        return loader(token, company, require_name=require_name)
    except Exception:
        log.info("ATS %s/%s failed", provider, token, exc_info=True)
        return None


def _load_greenhouse(token: str, company: str, *, require_name: bool) -> dict | None:
    meta = _fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{quote_plus(token)}", timeout=8)
    official = str((meta or {}).get("name") or "").strip()
    if not official and require_name:
        return None
    if official and not _names_match(company, official) and require_name:
        return None
    if official and not require_name and not _names_match(company, official) and not _url_matches(token, company):
        return None
    data = _fetch_json(
        f"https://boards-api.greenhouse.io/v1/boards/{quote_plus(token)}/jobs",
        timeout=30,
        max_bytes=4_000_000,
    )
    if not isinstance(data, dict) or "jobs" not in data:
        if official:
            return _pack("Greenhouse", f"https://boards.greenhouse.io/{token}", official, [], 0)
        return None
    rows = data.get("jobs") or []
    jobs = []
    display = official or company
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("absolute_url") or "").strip()
        title = str(row.get("title") or "").strip()
        if not url or not title:
            continue
        location = ((row.get("location") or {}) if isinstance(row.get("location"), dict) else {}).get("name") or ""
        job = _make_job(
            source="Greenhouse",
            job_id=str(row.get("id") or url.rstrip("/").split("/")[-1]),
            title=title,
            company=display,
            location=str(location),
            url=url,
            department=_label(row.get("departments")),
            posted_at=str(row.get("updated_at") or ""),
        )
        if job:
            jobs.append(job)
    return _pack("Greenhouse", f"https://boards.greenhouse.io/{token}", official, jobs, len(rows))


def _load_lever(token: str, company: str, *, require_name: bool) -> dict | None:
    del require_name
    data = _fetch_json(f"https://api.lever.co/v0/postings/{quote_plus(token)}?mode=json", timeout=12)
    if not isinstance(data, list):
        return None
    jobs = []
    for row in data:
        if not isinstance(row, dict):
            continue
        url = str(row.get("hostedUrl") or row.get("applyUrl") or "").strip()
        categories = row.get("categories") or {}
        job = _make_job(
            source="Lever",
            job_id=str(row.get("id") or ""),
            title=str(row.get("text") or ""),
            company=company,
            location=_location_text(categories.get("location") if isinstance(categories, dict) else ""),
            url=url,
            department=_label(categories.get("department") if isinstance(categories, dict) else ""),
            job_type=_label(categories.get("commitment") if isinstance(categories, dict) else ""),
            posted_at=_from_epoch(row.get("createdAt")),
            description=str(row.get("descriptionPlain") or ""),
        )
        if job:
            jobs.append(job)
    return _pack("Lever", f"https://jobs.lever.co/{token}", "", jobs, len(data))


def _load_ashby(token: str, company: str, *, require_name: bool) -> dict | None:
    del require_name
    data = _fetch_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{quote_plus(token)}",
        timeout=12,
        max_bytes=3_000_000,
    )
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    rows = data.get("jobs") or []
    jobs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        location = _location_text(row.get("location"))
        if row.get("isRemote") and "remote" not in location.casefold():
            location = f"{location} · Remote".strip(" ·")
        job = _make_job(
            source="Ashby",
            job_id=str(row.get("id") or ""),
            title=str(row.get("title") or ""),
            company=company,
            location=location,
            url=str(row.get("jobUrl") or row.get("applyUrl") or ""),
            department=_label(row.get("department") or row.get("team")),
            job_type=str(row.get("employmentType") or ""),
            posted_at=str(row.get("publishedAt") or ""),
            description=str(row.get("descriptionPlain") or ""),
        )
        if job:
            jobs.append(job)
    return _pack("Ashby", f"https://jobs.ashbyhq.com/{token}", "", jobs, len(rows))


def _load_smartrecruiters(token: str, company: str, *, require_name: bool) -> dict | None:
    meta = _fetch_json(f"https://api.smartrecruiters.com/v1/companies/{quote_plus(token)}", timeout=8)
    official = str((meta or {}).get("name") or "").strip()
    identifier = str((meta or {}).get("identifier") or token).strip() or token
    if require_name and (not official or not _names_match(company, official)):
        return None
    if official and not require_name and not _names_match(company, official) and not _url_matches(token, company):
        return None
    data = _fetch_json(
        f"https://api.smartrecruiters.com/v1/companies/{quote_plus(identifier)}/postings?limit=100",
        timeout=12,
    )
    if not isinstance(data, dict):
        return _pack("SmartRecruiters", f"https://jobs.smartrecruiters.com/{identifier}", official, [], 0) if official else None
    rows = data.get("content") or []
    total = int(data.get("totalFound") or len(rows))
    display = official or company
    jobs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        posting_id = str(row.get("id") or "")
        url = str(row.get("ref") or "")
        if not url.startswith("http"):
            url = f"https://jobs.smartrecruiters.com/{identifier}/{posting_id}"
        job = _make_job(
            source="SmartRecruiters",
            job_id=posting_id,
            title=str(row.get("name") or ""),
            company=display,
            location=_location_text(row.get("location")),
            url=url,
            department=_label(row.get("department")),
            job_type=_label(row.get("typeOfEmployment")),
            posted_at=str(row.get("releasedDate") or ""),
        )
        if job:
            jobs.append(job)
    return _pack("SmartRecruiters", f"https://jobs.smartrecruiters.com/{identifier}", official, jobs, total)


def _load_workable(token: str, company: str, *, require_name: bool) -> dict | None:
    data = _fetch_json(f"https://apply.workable.com/api/v1/widget/accounts/{quote_plus(token)}", timeout=10)
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    official = str(data.get("name") or "").strip()
    if require_name and (not official or not _names_match(company, official)):
        return None
    if official and not require_name and not _names_match(company, official) and not _url_matches(token, company):
        return None
    rows = data.get("jobs") or []
    display = official or company
    jobs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or row.get("shortlink") or "").strip()
        shortcode = str(row.get("shortcode") or "")
        if not url and shortcode:
            url = f"https://apply.workable.com/{token}/j/{shortcode}"
        job = _make_job(
            source="Workable",
            job_id=shortcode or url,
            title=str(row.get("title") or ""),
            company=display,
            location=_location_text(row.get("location") or row.get("locations")),
            url=url,
            department=_label(row.get("department")),
            job_type=str(row.get("employment_type") or row.get("type") or ""),
            posted_at=str(row.get("created_at") or row.get("published_on") or ""),
        )
        if job:
            jobs.append(job)
    return _pack("Workable", f"https://apply.workable.com/{token}/", official, jobs, len(rows))


def _load_recruitee(token: str, company: str, *, require_name: bool) -> dict | None:
    del require_name
    data = _fetch_json(f"https://{token}.recruitee.com/api/offers/", timeout=10)
    if not isinstance(data, dict) or "offers" not in data:
        return None
    rows = data.get("offers") or []
    jobs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("careers_url") or row.get("careers_apply_url") or "").strip()
        job = _make_job(
            source="Recruitee",
            job_id=str(row.get("id") or ""),
            title=str(row.get("title") or ""),
            company=company,
            location=_location_text(row.get("location") or row.get("locations") or row.get("city")),
            url=url,
            department=_label(row.get("department")),
            job_type=str(row.get("employment_type_code") or ""),
            posted_at=str(row.get("published_at") or row.get("created_at") or ""),
            description=str(row.get("description") or ""),
        )
        if job:
            jobs.append(job)
    return _pack("Recruitee", f"https://{token}.recruitee.com/", "", jobs, len(rows))


def _load_bamboohr(token: str, company: str, *, require_name: bool) -> dict | None:
    del require_name
    data = _fetch_json(f"https://{token}.bamboohr.com/careers/list", timeout=10)
    rows = []
    if isinstance(data, dict):
        rows = data.get("result") or data.get("jobs") or []
    elif isinstance(data, list):
        rows = data
    else:
        return None
    if not isinstance(rows, list):
        return None
    jobs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        job_id = str(row.get("id") or "")
        url = str(row.get("jobUrl") or row.get("url") or "")
        if not url and job_id:
            url = f"https://{token}.bamboohr.com/careers/{job_id}"
        job = _make_job(
            source="BambooHR",
            job_id=job_id or url,
            title=str(row.get("jobOpeningName") or row.get("title") or ""),
            company=company,
            location=str(row.get("locationLabel") or row.get("location") or ""),
            url=url,
            department=str(row.get("departmentLabel") or row.get("department") or ""),
            job_type=str(row.get("employmentStatusLabel") or ""),
        )
        if job:
            jobs.append(job)
    return _pack("BambooHR", f"https://{token}.bamboohr.com/careers", "", jobs, len(rows))


def _load_personio(token: str, company: str, *, require_name: bool) -> dict | None:
    del require_name
    page = _fetch(f"https://{token}.jobs.personio.de/xml", timeout=10, accept="application/xml,text/xml")
    if not page or page[0] != 200 or "<position" not in page[2].lower():
        return None
    try:
        root = ET.fromstring(page[2])
    except ET.ParseError:
        return None
    jobs = []
    for node in root.iter():
        if _local(node.tag) != "position":
            continue
        fields = {_local(child.tag): (child.text or "").strip() for child in list(node)}
        job_id = fields.get("id") or ""
        if not job_id or not fields.get("name"):
            continue
        job = _make_job(
            source="Personio",
            job_id=job_id,
            title=fields.get("name") or "",
            company=company,
            location=fields.get("office") or "",
            url=f"https://{token}.jobs.personio.de/job/{job_id}?language=en",
            department=fields.get("department") or "",
            job_type=fields.get("employmenttype") or "",
            posted_at=fields.get("createdat") or "",
        )
        if job:
            jobs.append(job)
    return _pack("Personio", f"https://{token}.jobs.personio.de/", "", jobs, len(jobs))


def _load_workday(info: dict, company: str) -> dict | None:
    tenant = str(info.get("tenant") or "").strip()
    cluster = str(info.get("cluster") or "").strip()
    site = str(info.get("site") or "").strip()
    if not tenant or not cluster or not site:
        return None
    host = f"{tenant}.{cluster}.myworkdayjobs.com"
    endpoint = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    jobs = []
    total = 0
    offset = 0
    seen = set()
    first = True
    while len(jobs) < 100:
        data = _fetch_json(
            endpoint,
            timeout=12,
            method="POST",
            payload={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
        )
        if not isinstance(data, dict):
            if first:
                return None
            break
        first = False
        total = int(data.get("total") or total or 0)
        postings = data.get("jobPostings") or []
        if not postings:
            break
        added = 0
        for row in postings:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            path = str(row.get("externalPath") or "").strip()
            if not title or not path:
                continue
            if not path.startswith("/"):
                path = "/" + path
            if f"/{site.lower()}/" in path.lower():
                url = f"https://{host}{path}"
            else:
                url = f"https://{host}/en-US/{site}{path}"
            if url in seen:
                continue
            seen.add(url)
            added += 1
            job = _make_job(
                source="Workday",
                job_id=path.rstrip("/").split("/")[-1][:80],
                title=title,
                company=company,
                location=str(row.get("locationsText") or ""),
                url=url,
                days_ago=_workday_when(str(row.get("postedOn") or "")),
            )
            if job:
                jobs.append(job)
        offset += len(postings)
        if added == 0 or (total and offset >= total):
            break
    career = str(info.get("url") or f"https://{host}/{site}")
    return _pack("Workday", career, "", jobs, total or len(jobs))


def _pack(portal: str, career_page: str, official: str, jobs: list[dict], total: int) -> dict:
    stored = jobs[:FETCH_CAP]
    return {
        "portal": portal,
        "career_page": career_page,
        "official_name": official,
        "jobs": stored,
        "total": max(total, len(jobs)),
    }


def _search_links(company: str) -> list[str]:
    queries = [f"{company} careers jobs"]
    token = _anchor_token(_tokens(company))
    if token and len(_tokens(company)) > 1 and token.casefold() != company.casefold():
        queries.append(f"{token} careers jobs")
    found: list[str] = []
    seen = set()
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="career-search")
    indexed = {pool.submit(_duckduckgo, query): index for index, query in enumerate(queries)}
    batches: list[list[str]] = [[] for _ in queries]
    try:
        for future in as_completed(indexed, timeout=12):
            try:
                batches[indexed[future]] = future.result() or []
            except Exception:
                log.info("Career search failed", exc_info=True)
    except TimeoutError:
        log.info("Career search timed out")
    _shutdown(pool)
    ordered: list[str] = []
    for batch in batches:
        ordered.extend(batch[:4])
    for batch in batches:
        ordered.extend(batch[4:])
    if not ordered:
        ordered = _bing(queries[0])
    for url in ordered:
        if not url or url in seen or not _safe_url(url) or _skipped(url):
            continue
        if "…" in url or "%E2%80%A6" in url or not _url_matches(url, company):
            continue
        seen.add(url)
        found.append(url)
        if len(found) >= 8:
            break
    found.sort(key=_link_rank)
    return found


def _duckduckgo(query: str) -> list[str]:
    page = _fetch(
        "https://html.duckduckgo.com/html/?q=" + quote_plus(query),
        timeout=8,
        accept="text/html",
    )
    if not page or page[0] != 200:
        return []
    links = []
    for href, _title in _RESULT_LINK.findall(page[2]):
        url = _unwrap_ddg(href)
        if url and url not in links:
            links.append(url)
    return links


def _bing(query: str) -> list[str]:
    page = _fetch("https://www.bing.com/search?q=" + quote_plus(query), timeout=8, accept="text/html")
    if not page or page[0] != 200:
        return []
    links = []
    for cite in re.findall(r"<cite[^>]*>(.*?)</cite>", page[2], re.I | re.S):
        plain = re.sub(r"<[^>]+>", "", html.unescape(cite))
        plain = plain.replace("›", "/").replace(">", "/")
        plain = re.sub(r"\s+", "", plain)
        if not plain:
            continue
        if not plain.startswith("http"):
            plain = "https://" + plain.lstrip("/")
        if plain not in links:
            links.append(plain)
    return links


def _unwrap_ddg(href: str) -> str:
    href = html.unescape(href or "")
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    target = parse_qs(parsed.query).get("uddg", [""])[0]
    if target:
        return unquote(target)
    if "duckduckgo.com" in (parsed.netloc or ""):
        return ""
    if href.startswith("http"):
        return href
    return ""


def _ats_from_url(url: str) -> dict | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = [part for part in parsed.path.split("/") if part]
    if host.endswith("greenhouse.io") and path:
        return {"provider": "greenhouse", "token": path[0]}
    if host.endswith("lever.co") and path:
        return {"provider": "lever", "token": path[0]}
    if host.endswith("ashbyhq.com") and path:
        return {"provider": "ashby", "token": path[0]}
    if host.endswith("smartrecruiters.com") and path:
        return {"provider": "smartrecruiters", "token": path[0]}
    if host.endswith("workable.com") and path:
        token = path[0]
        if token in {"api", "j"} and len(path) > 1:
            token = path[-1]
        return {"provider": "workable", "token": token}
    if host.endswith("bamboohr.com"):
        label = host.split(".")[0]
        if label and label not in {"www", "app"}:
            return {"provider": "bamboohr", "token": label}
    if host.endswith("recruitee.com"):
        label = host.split(".")[0]
        if label and label != "www":
            return {"provider": "recruitee", "token": label}
    if ".jobs.personio." in host:
        return {"provider": "personio", "token": host.split(".")[0]}
    workday = re.search(
        r"https?://(?P<tenant>[a-z0-9-]+)\.(?P<cluster>wd\d+)\.myworkdayjobs\.com/(?:(?P<locale>[a-z]{2}(?:-[a-zA-Z]{2})?)/)?(?P<site>[A-Za-z0-9_]+)",
        url,
        re.I,
    )
    if workday:
        return {
            "provider": "workday",
            "tenant": workday.group("tenant"),
            "cluster": workday.group("cluster"),
            "site": workday.group("site"),
            "url": url.split("?")[0],
        }
    return None


def _ats_from_html(page: str) -> dict | None:
    sample = page or ""
    for pattern, provider in (
        (r"boards\.greenhouse\.io/([A-Za-z0-9_-]+)", "greenhouse"),
        (r"job-boards\.greenhouse\.io/([A-Za-z0-9_-]+)", "greenhouse"),
        (r"greenhouse\.io/embed/job_board\?for=([A-Za-z0-9_-]+)", "greenhouse"),
        (r"jobs\.lever\.co/([A-Za-z0-9_-]+)", "lever"),
        (r"jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)", "ashby"),
        (r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)", "smartrecruiters"),
        (r"apply\.workable\.com/([A-Za-z0-9_-]+)", "workable"),
        (r"https?://([a-z0-9-]+)\.bamboohr\.com", "bamboohr"),
        (r"https?://([a-z0-9-]+)\.recruitee\.com", "recruitee"),
        (r"https?://([a-z0-9-]+)\.jobs\.personio\.(?:de|com)", "personio"),
    ):
        match = re.search(pattern, sample, re.I)
        if not match:
            continue
        token = match.group(1)
        if token.lower() in {"embed", "job_board", "www", "api", "jobs"}:
            continue
        return {"provider": provider, "token": token}
    workday = re.search(
        r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}(?:-[a-zA-Z]{2})/)?([A-Za-z0-9_]+)",
        sample,
        re.I,
    )
    if workday:
        return {
            "provider": "workday",
            "tenant": workday.group(1),
            "cluster": workday.group(2),
            "site": workday.group(3),
            "url": workday.group(0),
        }
    return None


def _make_job(
    *,
    source: str,
    job_id: str,
    title: str,
    company: str,
    location: str,
    url: str,
    department: str = "",
    job_type: str = "",
    posted_at: str = "",
    description: str = "",
    days_ago: str = "",
) -> dict | None:
    title = _plain(title, 180)
    url = _clean_url(url)
    if not title or not url:
        return None
    full_description = _plain(description, 2000)
    fields = infer_listing_fields(
        title=title,
        location=location or "",
        description=full_description,
        job_type=job_type or "",
    )
    posted = str(posted_at or "").strip()
    prefix = re.sub(r"[^a-z0-9]+", "-", source.casefold()).strip("-") or "job"
    ident = re.sub(r"\s+", "-", str(job_id or url)).strip("-")[:80] or "1"
    return {
        "id": f"{prefix}-{ident}"[:255],
        "title": title,
        "company": company,
        "location": _plain(location, 160),
        "department": _plain(department, 120),
        "url": url,
        "posted_at": posted,
        "daysAgo": days_ago or _ago_label(posted),
        "platform": source,
        "description": _plain(description, 500),
        **fields,
    }


def _fetch(url: str, *, timeout: int = 10, accept: str = "*/*", method: str = "GET", payload: dict | None = None, max_bytes: int = 1_500_000):
    target = _safe_url(url)
    if not target:
        return None
    headers = {"User-Agent": UA, "Accept": accept, "Accept-Language": "en-US,en;q=0.9"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = urllib.request.Request(target, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(max_bytes)
            return response.status, response.geturl(), body.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(min(max_bytes, 200_000))
        except Exception:
            body = b""
        return exc.code, target, body.decode("utf-8", "replace")
    except Exception as exc:
        log.info("Fetch failed %s (%s)", target, exc)
        return None


def _fetch_json(url: str, *, timeout: int = 10, method: str = "GET", payload: dict | None = None, max_bytes: int = 2_000_000):
    key = method + " " + url
    if payload is not None:
        key += " " + json.dumps(payload, sort_keys=True)
    with _JSON_LOCK:
        cached = _JSON_MEMO.get(key)
        if cached and cached[0] > time.time():
            return cached[1]
        event = _JSON_INFLIGHT.get(key)
        if event is None:
            event = threading.Event()
            _JSON_INFLIGHT[key] = event
            owner = True
        else:
            owner = False
    if not owner:
        event.wait(timeout + 5)
        with _JSON_LOCK:
            cached = _JSON_MEMO.get(key)
            return cached[1] if cached else None
    try:
        result = _fetch(
            url,
            timeout=timeout,
            accept="application/json",
            method=method,
            payload=payload,
            max_bytes=max_bytes,
        )
        value = None
        if result and result[0] == 200 and result[2]:
            try:
                value = json.loads(result[2])
            except json.JSONDecodeError:
                value = None
    except Exception:
        log.info("JSON fetch failed %s", url, exc_info=True)
        value = None
    with _JSON_LOCK:
        _JSON_MEMO[key] = (time.time() + (POSITIVE_TTL if value is not None else 30), value)
        _JSON_INFLIGHT.pop(key, None)
        if len(_JSON_MEMO) > 200:
            _JSON_MEMO.clear()
    event.set()
    return value


def _tokens(name: str) -> list[str]:
    parts = re.findall(r"[^\W_]+", (name or "").casefold(), flags=re.UNICODE)
    return [part for part in parts if part not in LEGAL and len(part) > 1]


def _anchor_token(parts: list[str]) -> str:
    if not parts:
        return ""
    return max(parts, key=len)


def _slugs(company: str) -> list[str]:
    parts = [re.sub(r"[^a-z0-9]", "", part) for part in _tokens(company)]
    parts = [part for part in parts if part]
    if not parts:
        return []
    values: list[str] = []

    def add(value: str) -> None:
        value = value.strip("-")
        if len(value) >= 2 and value not in values:
            values.append(value)

    add("".join(parts))
    if len(parts) > 1:
        add("-".join(parts))
    extra = _extra_slug(company)
    if extra:
        add(extra)
    return values[:3]


def _extra_slug(company: str) -> str:
    parts = [re.sub(r"[^a-z0-9]", "", part) for part in _tokens(company)]
    parts = [part for part in parts if part]
    if len(parts) < 2:
        return ""
    longest = max(parts, key=len)
    if len(longest) < 5 or longest in GENERIC_SLUG or longest == "".join(parts):
        return ""
    return longest


def _names_match(query: str, official: str) -> bool:
    left = set(_tokens(query))
    right = set(_tokens(official))
    if not left or not right:
        return False
    anchor = _anchor_token(list(left))
    if anchor and anchor in right:
        return True
    return len(left & right) / len(left) >= 0.5


def _url_matches(url: str, company: str) -> bool:
    token = _anchor_token(_tokens(company))
    if not token:
        return False
    labels = re.split(r"[^a-z0-9]+", url.casefold())
    for label in labels:
        if not label:
            continue
        if label == token:
            return True
        if len(token) >= 4 and (label.startswith(token) or label.endswith(token)):
            return True
    return False


def _host(url: str) -> str:
    name = (urlparse(url).hostname or "").lower()
    if name.startswith("www."):
        return name[4:]
    return name


def _skipped(url: str) -> bool:
    return any(label in SKIP_LABELS for label in _host(url).split("."))


def _careerish(url: str) -> bool:
    blob = f"{_host(url)} {urlparse(url).path}".casefold()
    return bool(re.search(r"career|jobs?|vacanc|opening|recruit|hiring", blob))


def _link_rank(url: str) -> int:
    path = urlparse(url).path.casefold()
    if any(bit in path for bit in ("viewall", "job-search", "opening", "vacanc", "/jobs", "/job")):
        return 0
    if "hiring" in path or "career" in path or "career" in _host(url):
        return 1
    return 2


def _safe_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "metadata.google.internal"} or host.endswith(".local"):
        return ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast):
        return ""
    return parsed.geturl()


def _clean_url(url: str) -> str:
    raw = html.unescape(url or "").strip()
    if "&" in raw and "?" not in raw:
        raw = raw.replace("&", "%26")
    target = _safe_url(raw)
    if not target:
        return ""
    parsed = urlparse(target)
    return parsed._replace(fragment="").geturl()


def _site_root(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return ""
    return f"{parsed.scheme}://{parsed.hostname}/"


def _is_job_url(url: str) -> bool:
    path = urlparse(url).path
    if not path or path == "/":
        return False
    lowered = path.casefold()
    if re.search(r"/jobs?/?$", lowered):
        return False
    if any(part in lowered for part in ("/job-search", "/search-results", "/search/")):
        return False
    return bool(_JOB_PATH.search(lowered))


def _same_family(url: str, page_url: str, company: str) -> bool:
    if _host(url) == _host(page_url):
        return True
    return _url_matches(url, company)


def _good_title(title: str) -> bool:
    text = title.casefold().strip()
    if text in NAV_TITLES or len(text) < 3:
        return False
    if text.startswith("expat"):
        return False
    return True


def _title_from_url(url: str) -> str:
    ignore = {"job", "jobs", "opening", "openings", "position", "positions", "vacancy", "vacancies", "go", "en", "ar", "search"}
    parts = [part for part in unquote(urlparse(url).path).split("/") if part]
    if parts and re.fullmatch(r"\d+", parts[-1]):
        parts = parts[:-1]
    for part in reversed(parts):
        if part.casefold() in ignore or part.casefold().startswith("expat"):
            continue
        if re.fullmatch(r"[a-z]{2}(?:[-_][a-z]{2})?", part.casefold()) and len(part) <= 5:
            continue
        title = html.unescape(part).replace("-", " ").replace("_", " ").strip()
        if _good_title(title):
            return title
    return ""


def _plain(value, limit: int) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _label(value) -> str:
    if isinstance(value, dict):
        return _plain(value.get("label") or value.get("name") or "", 120)
    if isinstance(value, list):
        for item in value:
            text = _label(item)
            if text:
                return text
        return ""
    return _plain(value, 120)


def _location_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_location_text(item) for item in value]
        return ", ".join(part for part in parts if part)
    if isinstance(value, dict):
        pieces = []
        for key in ("city", "region", "state", "country", "name"):
            piece = value.get(key)
            if isinstance(piece, dict):
                piece = piece.get("name") or piece.get("code")
            if piece:
                pieces.append(str(piece).strip())
        if value.get("remote") is True and not any("remote" in part.casefold() for part in pieces):
            pieces.append("Remote")
        return ", ".join(pieces)
    return str(value).strip()


def _jsonld_location(node: dict) -> str:
    location = node.get("jobLocation")
    if isinstance(location, list):
        return ", ".join(part for part in (_jsonld_place(item) for item in location) if part)
    return _jsonld_place(location)


def _jsonld_place(location) -> str:
    if isinstance(location, str):
        return location.strip()
    if not isinstance(location, dict):
        return ""
    address = location.get("address")
    if isinstance(address, str):
        return address.strip()
    if isinstance(address, dict):
        pieces = []
        for key in ("addressLocality", "addressRegion", "addressCountry"):
            piece = address.get(key)
            if isinstance(piece, dict):
                piece = piece.get("name")
            if piece:
                pieces.append(str(piece).strip())
        return ", ".join(pieces)
    return _plain(location.get("name") or "", 160)


def _employment(value) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    text = str(value or "")
    if "/" in text:
        text = text.rstrip("/").split("/")[-1]
    return text.replace("_", " ")


def _is_jobposting(node: dict) -> bool:
    kind = node.get("@type")
    if isinstance(kind, list):
        return any(str(item).casefold() == "jobposting" for item in kind)
    return str(kind or "").casefold() == "jobposting"


def _walk(payload):
    if isinstance(payload, list):
        for item in payload:
            yield from _walk(item)
        return
    if isinstance(payload, dict):
        yield payload
        graph = payload.get("@graph")
        if graph:
            yield from _walk(graph)


def _from_epoch(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number > 10_000_000_000:
        number /= 1000
    try:
        stamp = datetime.fromtimestamp(number, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return stamp.strftime("%Y-%m-%d %H:%M:%S")


def _ago_label(posted_at: str) -> str:
    text = (posted_at or "").strip()
    if not text:
        return ""
    text = text.replace("Z", "+00:00")
    if len(text) >= 11 and text[10] == " ":
        text = text[:10] + "T" + text[11:]
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - stamp).total_seconds())
    if seconds < 3600:
        return "today"
    if seconds < 86400:
        hours = max(seconds // 3600, 1)
        return "1 hour ago" if hours == 1 else f"{hours} hours ago"
    days = seconds // 86400
    if days < 30:
        return "1 day ago" if days == 1 else f"{days} days ago"
    months = days // 30
    if months < 12:
        return "1 month ago" if months == 1 else f"{months} months ago"
    years = max(days // 365, 1)
    return "1 year ago" if years == 1 else f"{years} years ago"


def _workday_when(text: str) -> str:
    raw = _plain(text, 80).casefold()
    if not raw:
        return ""
    if "today" in raw:
        return "today"
    if "yesterday" in raw:
        return "1 day ago"
    match = re.search(r"(\d+)\s*\+?\s*days?", raw)
    if not match:
        return ""
    count = int(match.group(1))
    if "+" in raw:
        return f"{count}+ days ago"
    return "1 day ago" if count == 1 else f"{count} days ago"


def _local(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1].casefold()


def _cache_get(key: str):
    with _CACHE_LOCK:
        row = _CACHE.get(key)
        if not row:
            return None
        expires, payload = row
        if expires < time.time():
            _CACHE.pop(key, None)
            return None
        return payload


def _cache_put(key: str, payload: dict, ttl: int) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) > 80:
            _CACHE.clear()
        _CACHE[key] = (time.time() + ttl, payload)


def _shutdown(pool: ThreadPoolExecutor) -> None:
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        pool.shutdown(wait=False)
