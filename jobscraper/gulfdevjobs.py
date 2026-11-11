"""Saudi tech listings from Gulf Dev Jobs.

The board publishes each listing page as HTML and the same URL plus ``.md``.
Saudi jobs are read from /jobs/in/saudi-arabia (about 20 cards per page).
Full descriptions are loaded from the markdown copy, a few dozen per sync,
so a first import fills in over later runs without pulling every page at once.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("jobscraper.gulfdevjobs")

API_BASE = "https://gulfdevjobs.com"
PLATFORM = "Gulf Dev Jobs"
_LIST_PATH = "/jobs/in/saudi-arabia"
_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0
_LAST: dict = {"ok": None, "saved": 0, "fetched": 0, "detailed": 0, "error": None, "at": None}

_ROW_RE = re.compile(r'<li class="job-row"[\s\S]*?</li>', re.I)
_SAVE_RE = re.compile(r'data-save-job="([^"]+)"', re.I)
_HREF_RE = re.compile(r'href="(/jobs/[a-z0-9]+(?:-[a-z0-9]+)+)"', re.I)
_SALARY_RE = re.compile(
    r"(?:SAR|AED|USD|QAR|KWD|BHD|OMR|GBP|EUR|£|€|\$)\s?\d[\d,.]*\s*k?"
    r"(?:\s*[–—-]\s*(?:SAR|AED|USD|QAR|KWD|BHD|OMR|GBP|EUR|£|€|\$)?\s?\d[\d,.]*\s*k?)?"
    r"(?:\s*/\s*(?:year|yr|month|mo|hour|hr|annum))?",
    re.I,
)
_POSTED_RE = re.compile(
    r"Posted\s+(today|yesterday|\d{1,2}\s+[A-Za-z]+\s+\d{4})",
    re.I,
)
_MD_POSTED_RE = re.compile(r"^Posted\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})", re.M)
_FACTS_RE = re.compile(r"^## Facts\s*(.*?)(?=^## |\Z)", re.M | re.S)
_DESCRIPTION_RE = re.compile(r"^## Job description\s*(.*?)(?=^## |\Z)", re.M | re.S)
_FACT_RE = re.compile(r"^-\s+([^:]+):\s*(.*)$")


def _env(name: str, default: str) -> str:
    return (os.getenv(name) or default).strip()


def _enabled() -> bool:
    return _env("GULFDEVJOBS_ENABLED", "true").lower() not in {"0", "false", "no", "off"}


def interval_minutes() -> int:
    try:
        minutes = int(_env("GULFDEVJOBS_INTERVAL_MINUTES", "60") or "60")
    except ValueError:
        minutes = 60
    return max(15, min(minutes, 24 * 60))


def _detail_budget() -> int:
    try:
        budget = int(_env("GULFDEVJOBS_DETAIL_BUDGET", "60") or "60")
    except ValueError:
        budget = 60
    return max(0, min(budget, 200))


def _max_pages() -> int:
    try:
        pages = int(_env("GULFDEVJOBS_MAX_PAGES", "25") or "25")
    except ValueError:
        pages = 25
    return max(1, min(pages, 40))


def _gap_seconds() -> float:
    try:
        gap = float(_env("GULFDEVJOBS_REQUEST_GAP_SECONDS", "0.4") or "0.4")
    except ValueError:
        gap = 0.4
    return max(0.2, min(gap, 5.0))


def gulfdevjobs_status() -> dict:
    return {
        "enabled": _enabled(),
        "interval_minutes": interval_minutes(),
        "detail_budget": _detail_budget(),
        "platform": PLATFORM,
        "attribution": "Source: Gulf Dev Jobs",
        "attribution_url": API_BASE,
        "list_url": f"{API_BASE}{_LIST_PATH}",
        "last": dict(_LAST),
    }


def _pace() -> None:
    global _LAST_REQUEST_AT
    gap = _gap_seconds()
    wait = gap - (time.monotonic() - _LAST_REQUEST_AT)
    if wait > 0:
        time.sleep(wait)
    _LAST_REQUEST_AT = time.monotonic()


def _get_text(url: str) -> str:
    _pace()
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html, text/markdown, text/plain;q=0.9",
            "User-Agent": "JobDiscoveryKSA/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read().decode("utf-8", "ignore")


def _visible(fragment: str) -> str:
    text = re.sub(r"<svg[\s\S]*?</svg>", " ", fragment or "", flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _posted_stamp(label: str) -> str:
    from jobscraper.agent_reach import absolute_posted_at

    text = (label or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"today|yesterday", text, re.I):
        return absolute_posted_at(text)
    for pattern in ("%d %B %Y", "%d %b %Y"):
        try:
            stamp = datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return stamp.strftime("%Y-%m-%d %H:%M:%S")
    return absolute_posted_at(text)


def _days_ago(posted: str) -> int:
    if not posted:
        return 0
    try:
        stamp = datetime.strptime(posted[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0
    return max(0, (datetime.now(timezone.utc) - stamp).days)


def _has_next(page_html: str, page: int) -> bool:
    return f"{_LIST_PATH}?page={page + 1}" in page_html or 'rel="next"' in page_html


def _cards(page_html: str) -> list[dict]:
    cards = []
    for chunk in _ROW_RE.findall(page_html or ""):
        save = _SAVE_RE.search(chunk)
        if not save:
            continue
        try:
            payload = json.loads(html.unescape(save.group(1)))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        slug = str(payload.get("slug") or "").strip().strip("/")
        href = _HREF_RE.search(chunk)
        if not slug and href:
            slug = href.group(1).rsplit("/", 1)[-1]
        title = str(payload.get("title") or "").strip()
        if not slug or not title:
            continue
        text = _visible(chunk)
        salary = _SALARY_RE.search(text)
        posted = _POSTED_RE.search(text)
        workplace = ""
        if re.search(r"\bHybrid\b", text):
            workplace = "Hybrid"
        elif re.search(r"\bOn-site\b", text):
            workplace = "On-site"
        cards.append(
            {
                "id": str(payload.get("id") or slug).strip(),
                "slug": slug,
                "title": title,
                "company": str(payload.get("company") or "Unknown").strip() or "Unknown",
                "location": str(payload.get("location") or "").strip(),
                "salary": salary.group(0).strip() if salary else "",
                "posted_label": posted.group(1).strip() if posted else "",
                "workplace": workplace,
                "visa": "Mentions visa sponsorship" in text,
                "url": f"{API_BASE}/jobs/{slug}",
            }
        )
    return cards


def list_pages() -> list[dict]:
    """Every Saudi card currently on the board. One HTML request per page."""
    found: list[dict] = []
    seen: set[str] = set()
    for page in range(1, _max_pages() + 1):
        url = f"{API_BASE}{_LIST_PATH}" if page == 1 else f"{API_BASE}{_LIST_PATH}?page={page}"
        page_html = _get_text(url)
        batch = _cards(page_html)
        if not batch:
            break
        for card in batch:
            if card["slug"] in seen:
                continue
            seen.add(card["slug"])
            found.append(card)
        if not _has_next(page_html, page):
            break
    return found


def _facts(markdown: str) -> dict[str, str]:
    match = _FACTS_RE.search(markdown or "")
    if not match:
        return {}
    facts: dict[str, str] = {}
    for line in match.group(1).splitlines():
        item = _FACT_RE.match(line.strip())
        if not item:
            continue
        key = item.group(1).strip().lower()
        value = item.group(2).strip()
        if key.startswith("not mentioned") or not value:
            continue
        facts[key] = value
    return facts


def _markdown_body(markdown: str) -> str:
    match = _DESCRIPTION_RE.search(markdown or "")
    if not match:
        return ""
    return re.sub(r"\n{3,}", "\n\n", match.group(1)).strip()


def _apply_detail(card: dict, markdown: str) -> None:
    facts = _facts(markdown)
    body = _markdown_body(markdown)
    workplace = facts.get("workplace") or card.get("workplace") or ""
    fact_location = facts.get("location") or ""
    if fact_location and not fact_location.lower().startswith("not "):
        card["location"] = fact_location
    salary = facts.get("salary") or card.get("salary") or ""
    if salary.lower().startswith("not "):
        salary = card.get("salary") or ""
    posted = _MD_POSTED_RE.search(markdown or "")
    lines = []
    if workplace:
        lines.append(f"Workplace: {workplace}")
    employment = facts.get("employment type") or ""
    if employment:
        lines.append(f"Employment type: {employment}")
    arabic = facts.get("arabic") or ""
    if arabic:
        lines.append(f"Arabic: {arabic}")
    if salary:
        lines.append(f"Salary: {salary}")
    if card.get("visa") or "visa sponsorship" in (facts.get("visa") or "").lower():
        lines.append("Mentions visa sponsorship")
    description = "\n".join(lines)
    if body:
        description = f"{description}\n\n{body}".strip() if description else body
    card["workplace"] = workplace
    card["salary"] = salary
    card["employment_type"] = employment
    card["arabic"] = arabic
    if posted:
        card["posted_label"] = posted.group(1)
    if description:
        card["description"] = description


_WORKPLACE_ONLY = {"on-site", "onsite", "on site", "hybrid", "remote", "fully remote"}


def _city_in_text(text: str) -> str:
    from jobscraper.locations import CITY_HINTS

    lower = (text or "").lower()
    for key, label in CITY_HINTS.items():
        if label == "Remote":
            continue
        if re.search(rf"\b{re.escape(key)}\b", lower):
            return label
    return ""


def _with_workplace(location: str, workplace: str) -> str:
    place = (location or "").strip()
    kind = (workplace or "").strip()
    if not kind:
        return place
    lower = place.lower()
    if kind.lower().startswith("remote") and "remote" not in lower:
        return f"{place} · Remote" if place else "Remote, Saudi Arabia"
    if kind.lower() == "hybrid" and "hybrid" not in lower:
        return f"{place} · Hybrid" if place else "Hybrid, Saudi Arabia"
    if kind.lower() in {"on-site", "onsite", "on site"} and not re.search(r"on[\s-]?site", lower):
        return f"{place} · On-site" if place else "On-site, Saudi Arabia"
    return place


def _saudi_location(card: dict) -> str:
    """The Saudi listing page is already country-filtered. A workplace-only card still counts."""
    from jobscraper.locations import is_ksa_job

    raw = str(card.get("location") or "").strip()
    workplace = str(card.get("workplace") or "").strip()
    if raw.lower() in _WORKPLACE_ONLY:
        if not workplace:
            workplace = raw
        raw = ""
    if raw and not is_ksa_job("", raw):
        return ""
    if not raw:
        city = _city_in_text(str(card.get("title") or ""))
        raw = f"{city}, Saudi Arabia" if city else "Saudi Arabia"
    return _with_workplace(raw, workplace)


def _to_job(card: dict) -> dict | None:
    from jobscraper.locations import parse_state

    title = str(card.get("title") or "").strip()
    slug = str(card.get("slug") or "").strip()
    if not title or not slug:
        return None
    location = _saudi_location(card)
    if not location:
        return None
    posted = _posted_stamp(str(card.get("posted_label") or ""))
    description = str(card.get("description") or "").strip()
    if not description:
        bits = []
        if card.get("workplace"):
            bits.append(f"Workplace: {card['workplace']}")
        if card.get("salary"):
            bits.append(f"Salary: {card['salary']}")
        if card.get("visa"):
            bits.append("Mentions visa sponsorship")
        description = "\n".join(bits)
    raw = {
        "slug": slug,
        "salary": card.get("salary") or "",
        "workplace": card.get("workplace") or "",
        "arabic": card.get("arabic") or "",
        "source": API_BASE,
    }
    return {
        "id": f"gulfdevjobs-{card.get('id') or slug}"[:255],
        "title": title,
        "company": str(card.get("company") or "Unknown").strip() or "Unknown",
        "skills": [],
        "state": parse_state(location),
        "location": location or "Saudi Arabia",
        "platform": PLATFORM,
        "url": str(card.get("url") or f"{API_BASE}/jobs/{slug}"),
        "description": description,
        "compensation": str(card.get("salary") or ""),
        "job_type": str(card.get("employment_type") or ""),
        "posted_at": posted,
        "platform_posted_at": posted,
        "daysAgo": _days_ago(posted),
        "raw": raw,
    }


def _described_ids() -> set[str]:
    try:
        from jobscraper.db import JOBS_TABLE, get_conn, mysql_configured

        if not mysql_configured():
            return set()
        conn = get_conn()
        if conn is None:
            return set()
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT source_id FROM {JOBS_TABLE} "
                "WHERE platform = %s AND CHAR_LENGTH(description) >= 200",
                (PLATFORM,),
            )
            rows = cur.fetchall() or []
    except Exception:
        log.exception("Could not read stored Gulf Dev Jobs descriptions")
        return set()
    found = set()
    for row in rows:
        source_id = row.get("source_id") if isinstance(row, dict) else None
        if source_id:
            found.add(str(source_id))
    return found


def pull_records() -> tuple[list[dict], int]:
    """List Saudi jobs, then load markdown for postings that are still short."""
    cards = list_pages()
    known = _described_ids()
    budget = _detail_budget()
    detailed = 0
    for card in cards:
        if detailed >= budget:
            break
        source_id = f"gulfdevjobs-{card.get('id') or card.get('slug')}"[:255]
        if source_id in known:
            continue
        try:
            markdown = _get_text(f"{card['url']}.md")
        except (urllib.error.URLError, TimeoutError, ValueError):
            log.exception("Gulf Dev Jobs detail failed for %s", card.get("slug"))
            continue
        if "## Job description" not in markdown and "## Facts" not in markdown:
            log.warning("Gulf Dev Jobs markdown missing for %s", card.get("slug"))
            continue
        _apply_detail(card, markdown)
        detailed += 1
    jobs = [job for job in (_to_job(card) for card in cards) if job]
    return jobs, detailed


def sync_gulf_jobs() -> dict:
    """Pull Saudi listings and upsert them. Safe to run hourly from the scheduler."""
    from jobscraper.sources import is_active, touch_last_sync

    if not _enabled():
        return {"ok": True, "saved": 0, "fetched": 0, "detailed": 0, "skipped": True}
    if not is_active("gulfdevjobs"):
        log.info("Gulf Dev Jobs sync skipped: source is inactive")
        return {"ok": True, "saved": 0, "fetched": 0, "detailed": 0, "skipped": True}
    if not _LOCK.acquire(blocking=False):
        log.info("Gulf Dev Jobs sync already running")
        return {"ok": True, "saved": 0, "fetched": 0, "detailed": 0, "skipped": True}
    saved = 0
    fetched = 0
    detailed = 0
    try:
        touch_last_sync("gulfdevjobs")
        jobs, detailed = pull_records()
        fetched = len(jobs)
        from jobscraper.db import mysql_configured, upsert_jobs

        if jobs and mysql_configured():
            saved = upsert_jobs(jobs).saved
        elif jobs:
            log.warning("Gulf Dev Jobs fetched %s rows but MySQL is not configured", fetched)
        _LAST.update(
            {
                "ok": True,
                "saved": saved,
                "fetched": fetched,
                "detailed": detailed,
                "error": None,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        log.info("Gulf Dev Jobs saved %s of %s (%s descriptions)", saved, fetched, detailed)
        return {"ok": True, "saved": saved, "fetched": fetched, "detailed": detailed}
    except Exception as exc:
        _LAST.update(
            {
                "ok": False,
                "saved": saved,
                "fetched": fetched,
                "detailed": detailed,
                "error": str(exc)[:500],
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        log.exception("Gulf Dev Jobs sync failed")
        return {"ok": False, "saved": saved, "fetched": fetched, "detailed": detailed, "error": str(exc)[:500]}
    finally:
        _LOCK.release()
