"""Tech events for a user's city and domain.

Riyadh Tech Week publishes the Saudi tech calendar for every city at
https://riyadhtechweek.com/events. The Riyadh guide adds fuller descriptions.
Sabtiah (https://sabtiah.cycls.com/) is Riyadh's weekly AI circle. A scrape
reads the whole Kingdom calendar, then keeps the events that fit the user's
city and domain.
"""

from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from jobscraper.locations import CITY_HINTS, REGIONS, region_name

log = logging.getLogger("jobscraper.events")

_ROOT = Path(__file__).resolve().parent.parent
STORE_FILE = _ROOT / "data" / "events.json"
RTW_BASE = "https://riyadhtechweek.com"
SABTIAH_URL = "https://sabtiah.cycls.com/"
RTW_SOURCE = "riyadhtechweek"
SABTIAH_SOURCE = "sabtiah"
_PAGE_SIZE = 24
_MAX_PAGES = 8
_LOCK = threading.Lock()

CREATE_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS tech_events (
  id BIGINT NOT NULL AUTO_INCREMENT,
  public_id VARCHAR(160) NOT NULL,
  source_id VARCHAR(64) NOT NULL,
  slug VARCHAR(255) NOT NULL,
  title VARCHAR(512) NOT NULL,
  city VARCHAR(120) NOT NULL,
  region VARCHAR(120) NOT NULL,
  venue VARCHAR(512) NOT NULL,
  location VARCHAR(512) NOT NULL,
  summary TEXT NOT NULL,
  category VARCHAR(64) NOT NULL,
  domains JSON NOT NULL,
  cost VARCHAR(16) NOT NULL,
  date_label VARCHAR(80) NOT NULL,
  when_label VARCHAR(80) NOT NULL,
  start_date DATE NULL,
  end_date DATE NULL,
  start_time VARCHAR(8) NOT NULL,
  end_time VARCHAR(8) NOT NULL,
  url VARCHAR(1000) NOT NULL,
  website VARCHAR(1000) NOT NULL,
  source VARCHAR(120) NOT NULL,
  scraped_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_tech_events_public_id (public_id),
  KEY idx_tech_events_city (city),
  KEY idx_tech_events_start (start_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

SOURCES = (
    {
        "id": RTW_SOURCE,
        "name": "Riyadh Tech Week",
        "url": f"{RTW_BASE}/events",
        "riyadh_url": f"{RTW_BASE}/events/riyadh",
        "location": "Saudi Arabia",
        "domains": [
            "AI",
            "Fintech",
            "Cybersecurity",
            "PropTech",
            "HealthTech",
            "Cloud & SaaS",
            "GovTech",
            "Deep Tech",
        ],
    },
    {
        "id": SABTIAH_SOURCE,
        "name": "Sabtiah",
        "url": SABTIAH_URL,
        "location": "Riyadh",
        "domains": ["AI"],
    },
)

_BROAD_LOCATIONS = {
    "",
    "all",
    "any",
    "ksa",
    "saudi",
    "saudi arabia",
    "kingdom of saudi arabia",
    "السعودية",
}
_BROAD_DOMAINS = {"", "all", "any", "tech", "technology", "technologies"}
_CATEGORIES = {
    "conference",
    "meetup",
    "hackathon",
    "summit",
    "expo",
    "roundtable",
    "webinar",
    "workshop",
    "networking",
    "forum",
    "festival",
    "exhibition",
    "demo day",
    "startup competition",
    "community",
    "builder",
    "other",
}
_COSTS = {"free", "paid"}
_DOMAIN_LABELS = {
    "ai",
    "data",
    "ai & data",
    "fintech",
    "cybersecurity",
    "cloud & saas",
    "web3",
    "healthtech",
    "proptech",
    "gaming",
    "gaming & esports",
    "devtools & infra",
    "enterprise tech",
    "govtech",
    "deep tech",
    "startups & entrepreneurship",
    "logistics & mobility",
    "logistics",
    "mobility",
    "saas",
    "climate & energy",
    "edtech",
    "ecommerce",
    "e-commerce",
    "foodtech",
    "hr tech",
}
_PRETTY_DOMAINS = {
    "ai": "AI",
    "data": "Data",
    "ai & data": "AI & Data",
    "fintech": "Fintech",
    "cybersecurity": "Cybersecurity",
    "cloud & saas": "Cloud & SaaS",
    "web3": "Web3",
    "healthtech": "HealthTech",
    "proptech": "PropTech",
    "gaming": "Gaming",
    "gaming & esports": "Gaming",
    "devtools & infra": "DevTools & Infra",
    "enterprise tech": "Enterprise Tech",
    "govtech": "GovTech",
    "deep tech": "Deep Tech",
    "startups & entrepreneurship": "Startups & Entrepreneurship",
    "logistics & mobility": "Logistics & Mobility",
    "logistics": "Logistics",
    "mobility": "Mobility",
    "saas": "SaaS",
    "climate & energy": "Climate & Energy",
    "edtech": "Edtech",
    "ecommerce": "Ecommerce",
    "e-commerce": "Ecommerce",
    "foodtech": "Foodtech",
    "hr tech": "HR Tech",
}
_DOMAIN_ALIASES = {
    "ai": ["ai", "ai & data"],
    "artificial intelligence": ["ai", "ai & data"],
    "data": ["data", "ai & data"],
    "fintech": ["fintech"],
    "cyber": ["cybersecurity"],
    "cybersecurity": ["cybersecurity"],
    "security": ["cybersecurity"],
    "proptech": ["proptech"],
    "prop tech": ["proptech"],
    "health": ["healthtech"],
    "healthtech": ["healthtech"],
    "health tech": ["healthtech"],
    "cloud": ["cloud & saas", "saas"],
    "saas": ["cloud & saas", "saas"],
    "web3": ["web3"],
    "gaming": ["gaming", "gaming & esports"],
    "devtools": ["devtools & infra"],
    "dev tools": ["devtools & infra"],
    "govtech": ["govtech"],
    "gov tech": ["govtech"],
    "enterprise": ["enterprise tech"],
    "enterprise tech": ["enterprise tech"],
    "deep tech": ["deep tech"],
    "deeptech": ["deep tech"],
    "logistics": ["logistics", "logistics & mobility"],
    "mobility": ["mobility", "logistics & mobility"],
    "startup": ["startups & entrepreneurship"],
    "startups": ["startups & entrepreneurship"],
}
_GUIDE_SLUGS = {"riyadh", "ai", "fintech", "cybersecurity", "hackathons"}
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_ARTICLE_RE = re.compile(r"<article\b[^>]*>(.*?)</article>", re.I | re.S)
_TITLE_RE = re.compile(
    r"<h3[^>]*>\s*<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>",
    re.I | re.S,
)
_GAP = r"(?:\s|<!--.*?-->)*"
_PAGE_OF_RE = re.compile(rf"Page{_GAP}(\d+){_GAP}of{_GAP}(\d+)", re.I | re.S)
_TOTAL_RE = re.compile(rf"of{_GAP}(\d+){_GAP}events", re.I | re.S)
_JSONLD_RE = re.compile(
    r"<script[^>]*type=\"application/ld\+json\"[^>]*>(.*?)</script>",
    re.I | re.S,
)
_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
_HREF_RE = re.compile(r"href=\"([^\"]+)\"", re.I)
_TIME_TAG_RE = re.compile(
    r"<time\b[^>]*datetime=\"([^\"]+)\"[^>]*>(.*?)</time>",
    re.I | re.S,
)
_HEADING_RE = re.compile(r"<h[1-3][^>]*>(.*?)</h[1-3]>", re.I | re.S)


class EventsError(Exception):
    """The event sources could not be read."""


def events_status() -> dict:
    stored = _read_store()
    return {
        "cached": len(stored.get("events") or []),
        "updated_at": stored.get("updated_at"),
        "sources": [
            {
                "id": row["id"],
                "name": row["name"],
                "url": row["url"],
                "location": row["location"],
                "domains": list(row["domains"]),
            }
            for row in SOURCES
        ],
    }


def event_filters() -> dict:
    cities = [
        "Riyadh",
        "Jeddah",
        "Makkah",
        "Taif",
        "Madinah",
        "Yanbu",
        "Dammam",
        "Khobar",
        "Dhahran",
        "Jubail",
        "Qatif",
        "Buraidah",
        "Abha",
        "Tabuk",
        "NEOM",
        "Hail",
        "Jazan",
        "Najran",
        "Thuwal",
        "Online",
    ]
    regions = [name for name in REGIONS if name not in cities]
    return {
        "locations": ["All", *cities, *regions],
        "domains": [
            "All",
            "AI",
            "Data",
            "Fintech",
            "Cybersecurity",
            "PropTech",
            "HealthTech",
            "Cloud & SaaS",
            "Web3",
            "Gaming",
            "GovTech",
            "Enterprise Tech",
            "Deep Tech",
            "Logistics",
            "Startups",
        ],
        "categories": [
            "Conference",
            "Meetup",
            "Hackathon",
            "Summit",
            "Expo",
            "Roundtable",
            "Webinar",
            "Workshop",
        ],
        "costs": ["free", "paid"],
        "sources": events_status()["sources"],
    }


def load_events() -> list[dict]:
    stored = list(_read_store().get("events") or [])
    if stored:
        return stored
    return _load_mysql() or []


def get_event(event_id: str) -> dict | None:
    wanted = (event_id or "").strip()
    if not wanted:
        return None
    for event in load_events():
        if event.get("id") == wanted:
            return event
    return None


def filter_events(
    events: list[dict],
    location: str = "",
    domain: str = "",
    category: str = "",
    cost: str = "",
    q: str = "",
) -> list[dict]:
    category_key = (category or "").strip().lower()
    cost_key = (cost or "").strip().lower()
    query = (q or "").strip().lower()
    matched = []
    for event in events:
        if not location_matches(event.get("city") or "", location):
            continue
        if not domain_matches(event, domain):
            continue
        if category_key and (event.get("category") or "").lower() != category_key:
            continue
        if cost_key in _COSTS and (event.get("cost") or "") != cost_key:
            continue
        if query and query not in _search_blob(event):
            continue
        matched.append(event)
    matched.sort(key=_sort_key)
    return matched


def scrape_events(
    location: str = "",
    domain: str = "",
    *,
    fetch=None,
    store_path: Path | None = None,
    polite: bool = True,
    today: date | None = None,
) -> dict:
    """Read the sources for this location and domain, save them, and return matches."""
    fetcher = fetch or _fetch
    today = today or datetime.now(timezone.utc).date()
    wanted = _sources_for(location, domain)
    if not wanted:
        return {
            "events": [],
            "stored": len(_read_store(store_path).get("events") or []),
            "scraped_at": None,
            "sources": [],
            "note": "No event source covers that location.",
        }
    fresh: list[dict] = []
    scopes: list[dict] = []
    reports = []
    for source_id in wanted:
        if source_id == RTW_SOURCE:
            events, report, scope = _scrape_rtw(location, fetcher, polite, today)
        else:
            events, report, scope = _scrape_sabtiah(fetcher, polite, today)
        reports.append(report)
        if report["ok"]:
            fresh.extend(events)
            scopes.append(scope)
    if not any(report["ok"] for report in reports):
        detail = "; ".join(
            f"{report['name']}: {report['error']}" for report in reports if report["error"]
        )
        raise EventsError(detail or "Event sources could not be read.")
    with _LOCK:
        existing = _read_store(store_path).get("events") or []
        merged = _merge(existing, fresh, scopes)
        scraped_at = datetime.now(timezone.utc).isoformat()
        _write_store({"updated_at": scraped_at, "events": merged}, store_path)
    _save_mysql(merged, scopes, scraped_at)
    matched = filter_events(merged, location, domain)
    return {
        "events": matched,
        "stored": len(merged),
        "scraped_at": scraped_at,
        "sources": reports,
    }


def location_matches(event_city: str, query: str) -> bool:
    text = (query or "").strip()
    if text.lower() in _BROAD_LOCATIONS:
        return True
    if _same_place(event_city, text):
        return True
    region = _region_query(text)
    if region and region_name(event_city) == region:
        return True
    return False


def domain_matches(event: dict, domain: str) -> bool:
    key = _normalize_domain(domain)
    if key in _BROAD_DOMAINS:
        return True
    tags = [str(tag).lower() for tag in event.get("domains") or []]
    category = str(event.get("category") or "").lower()
    title = str(event.get("title") or "").lower()
    summary = str(event.get("summary") or "").lower()
    needles = _DOMAIN_ALIASES.get(key)
    if needles is None:
        if key in _CATEGORIES:
            return category == key
        blob = " ".join(tags + [category, title])
        if len(key) <= 3:
            return re.search(rf"(?<![a-z]){re.escape(key)}(?![a-z])", blob) is not None
        return key in blob
    if any(_tag_hit(tag, needle) for tag in tags for needle in needles):
        return True
    if any(_tag_hit(title, needle) for needle in needles):
        return True
    return any(len(needle) > 3 and needle in summary for needle in needles)


def parse_listing_html(page_html: str, page_url: str, today: date | None = None) -> list[dict]:
    today = today or datetime.now(timezone.utc).date()
    events = []
    for article in _ARTICLE_RE.findall(page_html or ""):
        event = _parse_article(article, page_url, today)
        if event:
            events.append(event)
    return events


def parse_date_label(label: str, slug: str = "", today: date | None = None) -> tuple[str, str]:
    today = today or datetime.now(timezone.utc).date()
    text = _normalize_dash(label)
    text = re.sub(r"\d{1,2}:\d{2}(?:\s*-\s*\d{1,2}:\d{2})?", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -")
    year_match = re.search(r"(20\d{2})", slug or "")
    fallback_year = int(year_match.group(1)) if year_match else today.year

    ranged = re.fullmatch(
        r"(\d{1,2}) ([A-Za-z]+) - (\d{1,2}) ([A-Za-z]+)(?: (\d{4}))?",
        text,
    )
    if ranged:
        start = _make_date(int(ranged.group(1)), ranged.group(2), int(ranged.group(5) or fallback_year))
        end_year = int(ranged.group(5) or fallback_year)
        end = _make_date(int(ranged.group(3)), ranged.group(4), end_year)
        if start and end and end < start and not ranged.group(5):
            end = _make_date(int(ranged.group(3)), ranged.group(4), end_year + 1)
        return _iso(start), _iso(end)

    same_month = re.fullmatch(r"(\d{1,2})-(\d{1,2}) ([A-Za-z]+)(?: (\d{4}))?", text)
    if same_month:
        year = int(same_month.group(4) or fallback_year)
        start = _make_date(int(same_month.group(1)), same_month.group(3), year)
        end = _make_date(int(same_month.group(2)), same_month.group(3), year)
        return _iso(start), _iso(end)

    single = re.fullmatch(r"(\d{1,2}) ([A-Za-z]+)(?: (\d{4}))?", text)
    if single:
        year = int(single.group(3) or fallback_year)
        found = _make_date(int(single.group(1)), single.group(2), year)
        iso = _iso(found)
        return iso, iso
    return "", ""


def _scrape_rtw(location: str, fetch, polite: bool, today: date):
    """Read every Saudi city, then add the longer Riyadh descriptions."""
    del location  # The city filter is applied after the Kingdom calendar is saved.
    directory, pages, error = _walk_pages(f"{RTW_BASE}/events", fetch, polite, today, parse_listing_html)
    guide, guide_pages, guide_error = _walk_pages(
        f"{RTW_BASE}/events/riyadh", fetch, polite, today, parse_listing_html
    )
    events = _merge_listings(directory, guide)
    problems = [item for item in (error, guide_error) if item]
    report = {
        "id": RTW_SOURCE,
        "name": "Riyadh Tech Week",
        "url": f"{RTW_BASE}/events",
        "riyadh_url": f"{RTW_BASE}/events/riyadh",
        "fetched": len(events),
        "pages": pages + guide_pages,
        "cities": sorted({event["city"] for event in events if event.get("city")}),
        "ok": bool(events),
        "error": "; ".join(problems) if problems and not events else None,
    }
    if events and problems:
        report["error"] = "; ".join(problems)
    return events, report, {"source_id": RTW_SOURCE, "cities": None}


def _merge_listings(primary: list[dict], extra: list[dict]) -> list[dict]:
    """Keep one row per event. Fill empty Riyadh fields from the city guide."""
    by_id = {event["id"]: event for event in primary}
    for event in extra:
        current = by_id.get(event["id"])
        if current is None:
            by_id[event["id"]] = event
            continue
        for field in ("summary", "start_time", "end_time", "when_label", "website", "venue"):
            if not current.get(field) and event.get(field):
                current[field] = event[field]
        if current.get("cost") in {"", "unknown"} and event.get("cost") not in {"", "unknown", None}:
            current["cost"] = event["cost"]
        if not current.get("category") and event.get("category"):
            current["category"] = event["category"]
        if not current.get("start_date") and event.get("start_date"):
            current["start_date"] = event["start_date"]
            current["end_date"] = event.get("end_date") or event["start_date"]
            current["date_label"] = event.get("date_label") or current.get("date_label") or ""
        domains = list(current.get("domains") or [])
        have = {item.lower() for item in domains}
        for domain in event.get("domains") or []:
            if domain.lower() not in have:
                domains.append(domain)
                have.add(domain.lower())
        current["domains"] = domains
    return list(by_id.values())


def _scrape_sabtiah(fetch, polite: bool, today: date):
    pages = [SABTIAH_URL.rstrip("/")]
    seen = set()
    events: list[dict] = []
    error = None
    fetched_pages = 0
    while pages and fetched_pages < 6:
        url = pages.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if polite and fetched_pages:
            time.sleep(0.2)
        try:
            page_html = fetch(url)
        except Exception as exc:
            error = _error_text(exc)
            if fetched_pages == 0:
                break
            continue
        fetched_pages += 1
        events.extend(_parse_sabtiah(page_html, url, today))
        if fetched_pages == 1:
            pages.extend(_sabtiah_links(page_html, url))
    deduped = _dedupe(events)
    ok = error is None or bool(deduped)
    note = None
    if ok and not deduped:
        note = "Sabtiah's public pages did not list a dated session."
    report = {
        "id": SABTIAH_SOURCE,
        "name": "Sabtiah",
        "url": SABTIAH_URL,
        "fetched": len(deduped),
        "pages": fetched_pages,
        "ok": ok and fetched_pages > 0,
        "error": None if fetched_pages else error,
        "note": note,
    }
    return deduped, report, {"source_id": SABTIAH_SOURCE, "cities": None}


def _walk_pages(first_url: str, fetch, polite: bool, today: date, parser) -> tuple[list[dict], int, str | None]:
    events: list[dict] = []
    error = None
    pages = 0
    total_pages = 1
    while pages < total_pages and pages < _MAX_PAGES:
        url = first_url if pages == 0 else f"{first_url}?page={pages + 1}"
        if polite and pages:
            time.sleep(0.2)
        try:
            page_html = fetch(url)
        except Exception as exc:
            error = _error_text(exc)
            break
        pages += 1
        events.extend(parser(page_html, url, today))
        total_pages = max(total_pages, _page_count(page_html))
    if pages == 0:
        return [], 0, error
    return _dedupe(events), pages, error


def _page_count(page_html: str) -> int:
    match = _PAGE_OF_RE.search(page_html or "")
    if match:
        return max(1, min(int(match.group(2)), _MAX_PAGES))
    total = _TOTAL_RE.search(page_html or "")
    if total:
        count = int(total.group(1))
        return max(1, min((count + _PAGE_SIZE - 1) // _PAGE_SIZE, _MAX_PAGES))
    return 1


def _parse_article(article: str, page_url: str, today: date) -> dict | None:
    title_match = _TITLE_RE.search(article)
    if not title_match:
        return None
    href = html.unescape(title_match.group(1)).strip()
    slug = href.rstrip("/").rsplit("/", 1)[-1]
    if not slug or slug in _GUIDE_SLUGS:
        return None
    title = _clean(title_match.group(2))
    if not title:
        return None
    if 'class="text-foreground"' in article:
        return _parse_guide_article(article, href, slug, title, page_url, today)
    if "leading-tight" in article:
        return _parse_directory_article(article, href, slug, title, page_url, today)
    return None


def _parse_guide_article(article, href, slug, title, page_url, today) -> dict | None:
    date_match = re.search(r'class="text-foreground">(.*?)</span>', article, re.S)
    date_label = _clean(date_match.group(1)) if date_match else ""
    when = ""
    when_match = re.search(r'class="text-muted-foreground">(.*?)</span>', article, re.S)
    if when_match:
        when = _clean(when_match.group(1)).lstrip("·").strip()
    time_match = re.search(r'<div class="mt-1">(.*?)</div>', article, re.S)
    start_time, end_time = _parse_clock(_clean(time_match.group(1)) if time_match else "")
    venue, city = _split_place(_meta_text(article, r'<p class="mt-1 text-sm text-muted-foreground">(.*?)</p>'))
    summary_match = re.search(r'<p class="mt-2 text-sm">(.*?)</p>', article, re.S)
    summary = _clean(summary_match.group(1)) if summary_match else ""
    tags = [_clean(tag) for tag in re.findall(r'<span class="label-xs[^"]*"[^>]*>(.*?)</span>', article, re.S)]
    category, domains, cost = _classify_tags(tags)
    return _event(
        source_id=RTW_SOURCE,
        source="Riyadh Tech Week",
        slug=slug,
        title=title,
        href=href,
        page_url=page_url,
        venue=venue,
        city=city,
        summary=summary,
        category=category,
        domains=domains,
        cost=cost,
        date_label=date_label,
        when_label=when,
        start_time=start_time,
        end_time=end_time,
        website=_external_link(article),
        today=today,
    )


def _parse_directory_article(article, href, slug, title, page_url, today) -> dict | None:
    column = re.search(r'<div class="min-w-0">(.*?)</div>', article, re.S)
    column_text = _clean(column.group(1)) if column else ""
    date_match = re.search(r'leading-tight">(.*?)</p>', article, re.S)
    date_label = _clean(date_match.group(1)) if date_match else ""
    status_match = re.search(r'text-primary">(.*?)</p>', article, re.S)
    when = _clean(status_match.group(1)) if status_match else ""
    start_time, end_time = _parse_clock(column_text)
    venue, city, category, domains, cost = _parse_directory_meta(
        _meta_text(article, r"normal-case tracking-wide\">(.*?)</p>")
    )
    return _event(
        source_id=RTW_SOURCE,
        source="Riyadh Tech Week",
        slug=slug,
        title=title,
        href=href,
        page_url=page_url,
        venue=venue,
        city=city,
        summary="",
        category=category,
        domains=domains,
        cost=cost,
        date_label=date_label,
        when_label=when,
        start_time=start_time,
        end_time=end_time,
        website=_external_link(article),
        today=today,
    )


def _parse_directory_meta(meta: str) -> tuple[str, str, str, list[str], str]:
    parts = [part.strip() for part in re.split(r"\s·\s", meta) if part.strip()]
    tags: list[str] = []
    while parts and _is_meta_tag(parts[-1]):
        tags.append(parts.pop())
    tags.reverse()
    category, domains, cost = _classify_tags(tags)
    if len(parts) >= 2:
        venue, city = parts[0], parts[1]
    elif len(parts) == 1:
        venue, city = parts[0], ""
    else:
        venue, city = "", ""
    return venue, city, category, domains, cost


def _event(
    *,
    source_id: str,
    source: str,
    slug: str,
    title: str,
    href: str,
    page_url: str,
    venue: str,
    city: str,
    summary: str,
    category: str,
    domains: list[str],
    cost: str,
    date_label: str,
    when_label: str,
    start_time: str,
    end_time: str,
    website: str,
    today: date,
) -> dict:
    start_date, end_date = parse_date_label(date_label, slug, today)
    city_name = _display_city(city)
    venue_name = venue.strip()
    if venue_name and city_name:
        place = f"{venue_name} · {city_name}"
    else:
        place = venue_name or city_name
    url = _absolute(href, page_url)
    return {
        "id": f"{source_id}__{slug}",
        "slug": slug,
        "title": title,
        "city": city_name,
        "region": region_name(city_name) or ("Remote" if _city_key(city_name) == "online" else ""),
        "venue": venue_name,
        "location": place,
        "start_date": start_date,
        "end_date": end_date,
        "date_label": date_label,
        "when_label": when_label,
        "start_time": start_time,
        "end_time": end_time,
        "category": category,
        "domains": _infer_domains(title, summary, domains),
        "cost": cost,
        "summary": summary[:2000],
        "url": url,
        "website": website,
        "source": source,
        "source_id": source_id,
    }


def _parse_sabtiah(page_html: str, page_url: str, today: date) -> list[dict]:
    events = []
    for node in _jsonld_events(page_html):
        title = str(node.get("name") or "").strip()
        if not title:
            continue
        start_raw = str(node.get("startDate") or "")
        end_raw = str(node.get("endDate") or start_raw)
        start_date, start_time = _split_iso(start_raw)
        end_date, end_time = _split_iso(end_raw)
        venue, city = _jsonld_place(node.get("location"))
        url = str(node.get("url") or page_url)
        slug = _slug_from_url(url) or _slugify(title)
        event = _event(
            source_id=SABTIAH_SOURCE,
            source="Sabtiah",
            slug=slug,
            title=title,
            href=url,
            page_url=page_url,
            venue=venue or "Sabtiah",
            city=city or "Riyadh",
            summary=_clean(str(node.get("description") or ""))[:2000],
            category="Meetup",
            domains=["AI"],
            cost="free",
            date_label=start_date,
            when_label="",
            start_time=start_time,
            end_time=end_time if end_date else "",
            website=url if url.startswith("http") else "",
            today=today,
        )
        event["start_date"] = start_date
        event["end_date"] = end_date or start_date
        event["date_label"] = start_date
        events.append(event)
    events.extend(_parse_liqa_sessions(page_html, today))
    if events:
        return events
    return _parse_sabtiah_times(page_html, page_url, today)


def _parse_liqa_sessions(page_html: str, today: date) -> list[dict]:
    """Sabtiah publishes Saturday sessions on liqa.io inside the page payload."""
    text = (page_html or "").replace('\\"', '"')
    pattern = re.compile(
        r'"title":"(?P<title>(?:\\.|[^"\\])*)".{0,900}?'
        r'"slug":"(?P<slug>[A-Za-z0-9]+)".{0,500}?'
        r'"startTimeStamp":"\$D(?P<start>[^"]+)".{0,240}?'
        r'"endTimeStamp":"\$D(?P<end>[^"]+)".{0,400}?'
        r'"city":"(?P<city>(?:\\.|[^"\\])*)".{0,160}?'
        r'"isPast":(?P<past>true|false)',
        re.S,
    )
    events = []
    for match in pattern.finditer(text):
        if match.group("past") == "true":
            continue
        start_date, start_time = _in_riyadh(match.group("start"))
        end_date, end_time = _in_riyadh(match.group("end"))
        if not start_date:
            continue
        raw_title = match.group("title")
        title = "Sabtiah — Riyadh's AI Circle" if "السبتية" in raw_title or "sabtiah" in raw_title.lower() else raw_title
        venue, city = _sabtiah_place(match.group("city"))
        slug = match.group("slug")
        url = f"https://liqa.io/events/{slug}"
        event = _event(
            source_id=SABTIAH_SOURCE,
            source="Sabtiah",
            slug=slug.lower(),
            title=title,
            href=url,
            page_url=url,
            venue=venue,
            city=city,
            summary=raw_title,
            category="Meetup",
            domains=["AI"],
            cost="unknown",
            date_label=start_date,
            when_label="",
            start_time=start_time,
            end_time=end_time,
            website=SABTIAH_URL,
            today=today,
        )
        event["start_date"] = start_date
        event["end_date"] = end_date or start_date
        event["date_label"] = start_date
        events.append(event)
    return events


def _sabtiah_place(city_text: str) -> tuple[str, str]:
    if "الرياض" in city_text or "riyadh" in city_text.lower():
        venue = "Sabtiah, Al Malqa" if "الملقا" in city_text or "malqa" in city_text.lower() else "Sabtiah"
        return venue, "Riyadh"
    return city_text or "Sabtiah", "Riyadh"


def _in_riyadh(stamp: str) -> tuple[str, str]:
    text = (stamp or "").strip().replace("Z", "+00:00")
    if not text:
        return "", ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return "", ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone(ZoneInfo("Asia/Riyadh"))
    return local.date().isoformat(), local.strftime("%H:%M")


def _parse_sabtiah_times(page_html: str, page_url: str, today: date) -> list[dict]:
    heading = ""
    for match in _HEADING_RE.finditer(page_html or ""):
        text = _clean(match.group(1))
        if text and "where builders gather" not in text.lower():
            heading = text
            break
    found = []
    for match in _TIME_TAG_RE.finditer(page_html or ""):
        start_date, start_time = _split_iso(match.group(1))
        if not start_date:
            continue
        label = _clean(match.group(2)) or heading or "Sabtiah session"
        slug = f"sabtiah-{start_date}"
        event = _event(
            source_id=SABTIAH_SOURCE,
            source="Sabtiah",
            slug=slug,
            title=heading or "Sabtiah — Riyadh's AI Circle",
            href=page_url,
            page_url=page_url,
            venue="Sabtiah, Al Malqa",
            city="Riyadh",
            summary=label,
            category="Meetup",
            domains=["AI"],
            cost="free",
            date_label=start_date,
            when_label="",
            start_time=start_time,
            end_time="",
            website=page_url,
            today=today,
        )
        event["start_date"] = start_date
        event["end_date"] = start_date
        found.append(event)
    return found


def _sabtiah_links(page_html: str, page_url: str) -> list[str]:
    links = []
    for attrs, inner in _ANCHOR_RE.findall(page_html or ""):
        href_match = _HREF_RE.search(attrs)
        if not href_match:
            continue
        href = html.unescape(href_match.group(1)).strip()
        label = _clean(inner).lower()
        absolute = _absolute(href, page_url)
        if not absolute.startswith("http"):
            continue
        host = urllib.request.urlparse(absolute).netloc.lower()
        interesting = any(
            word in label
            for word in ("save your seat", "open session", "next session", "book", "rsvp", "session")
        )
        same_site = "sabtiah.cycls.com" in host and (
            interesting or any(part in absolute.lower() for part in ("/session", "/book", "/open", "/saturday"))
        )
        rsvp = "liqa.io" in host and (interesting or absolute.rstrip("/").endswith("/sabtiah"))
        if (same_site or rsvp) and absolute.rstrip("/") != page_url.rstrip("/"):
            links.append(absolute.split("#")[0])
    ordered = []
    for link in links:
        if link not in ordered:
            ordered.append(link)
    return ordered[:5]


def _jsonld_events(page_html: str):
    for block in _JSONLD_RE.findall(page_html or ""):
        try:
            payload = json.loads(html.unescape(block).strip())
        except json.JSONDecodeError:
            continue
        yield from _walk_events(payload)


def _walk_events(node):
    if isinstance(node, list):
        for item in node:
            yield from _walk_events(item)
        return
    if not isinstance(node, dict):
        return
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    if any(str(item or "").lower() in {"event", "socialevent", "educationevent", "businessevent"} for item in kinds):
        yield node
    for value in node.values():
        if isinstance(value, (dict, list)):
            yield from _walk_events(value)


def _jsonld_place(location) -> tuple[str, str]:
    if isinstance(location, str):
        return location, "Riyadh" if "riyadh" in location.lower() else ""
    if isinstance(location, dict):
        name = str(location.get("name") or "")
        address = location.get("address")
        city = ""
        if isinstance(address, dict):
            city = str(address.get("addressLocality") or "")
        elif isinstance(address, str) and "riyadh" in address.lower():
            city = "Riyadh"
        if "riyadh" in name.lower():
            city = city or "Riyadh"
        return name, city
    return "", ""


def _sources_for(location: str, domain: str) -> list[str]:
    sources = []
    if _in_saudi_scope(location):
        sources.append(RTW_SOURCE)
    if _wants_sabtiah(location, domain):
        sources.append(SABTIAH_SOURCE)
    return sources


def _in_saudi_scope(location: str) -> bool:
    text = (location or "").strip()
    if text.lower() in _BROAD_LOCATIONS:
        return True
    return bool(_place_token(text) or _region_query(text))


def _wants_sabtiah(location: str, domain: str) -> bool:
    text = (location or "").strip()
    if text and text.lower() not in _BROAD_LOCATIONS and _place_token(text) != "riyadh":
        return False
    key = _normalize_domain(domain)
    return key in _BROAD_DOMAINS or key in {"ai", "data"}


def _region_query(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if text in REGIONS:
        return text
    for name in REGIONS:
        if text.lower() == name.lower():
            return name
    return ""


def _same_city(left: str, right: str) -> bool:
    return _same_place(left, right)


def _same_place(left: str, right: str) -> bool:
    a = _place_token(left)
    b = _place_token(right)
    return bool(a and b and a == b)


def _place_token(value: str) -> str:
    key = _city_key(value)
    if key:
        return key
    text = re.sub(r"\s+", " ", (value or "").strip().lower())
    for cities in REGIONS.values():
        for city in cities:
            if text == city.lower():
                return "riyadh" if city == "الرياض" else text
    return ""


def _city_key(value: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip().lower()).replace("-", " ")
    aliases = {
        "riyadh": "riyadh",
        "الرياض": "riyadh",
        "jeddah": "jeddah",
        "jedda": "jeddah",
        "jidda": "jeddah",
        "جدة": "jeddah",
        "makkah": "makkah",
        "mecca": "makkah",
        "khobar": "khobar",
        "al khobar": "khobar",
        "khubar": "khobar",
        "al khubar": "khobar",
        "الخبر": "khobar",
        "dammam": "dammam",
        "الدمام": "dammam",
        "dhahran": "dhahran",
        "الظهران": "dhahran",
        "thuwal": "thuwal",
        "online": "online",
        "virtual": "online",
        "remote": "online",
    }
    if text in aliases:
        return aliases[text]
    hinted = CITY_HINTS.get(text)
    if hinted and hinted != "Remote":
        return aliases.get(hinted.lower(), hinted.lower())
    if text == "remote":
        return "online"
    return ""


def _display_city(city: str) -> str:
    key = _city_key(city)
    names = {
        "riyadh": "Riyadh",
        "jeddah": "Jeddah",
        "makkah": "Makkah",
        "khobar": "Khobar",
        "dammam": "Dammam",
        "dhahran": "Dhahran",
        "thuwal": "Thuwal",
        "online": "Online",
    }
    if key in names:
        return names[key]
    cleaned = re.sub(r"\s+", " ", (city or "")).strip()
    if cleaned.lower() in {"saudi arabia", "ksa"}:
        return "Saudi Arabia"
    return cleaned


def _normalize_domain(value: str) -> str:
    text = (value or "").replace("&", " ").replace("/", " ").replace("_", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text).strip().lower()
    folded = {
        "prop tech": "proptech",
        "health tech": "healthtech",
        "cyber security": "cybersecurity",
        "gov tech": "govtech",
        "dev tools": "devtools",
        "deeptech": "deep tech",
        "artificial intelligence": "ai",
    }
    return folded.get(text, text)


def _infer_domains(title: str, summary: str, domains: list[str]) -> list[str]:
    """Keep the site's topic tags, and add a topic named in the title or summary."""
    found = list(domains)
    have = {item.lower() for item in found}
    title_text = title.lower()
    summary_text = summary.lower()
    for label, pretty in _PRETTY_DOMAINS.items():
        if label in have or pretty.lower() in have:
            continue
        if label in {"ai", "data"} and "ai & data" in have:
            continue
        if _tag_hit(title_text, label) or (len(label) > 3 and label in summary_text):
            found.append(pretty)
            have.add(label)
    return found


def _classify_tags(tags: list[str]) -> tuple[str, list[str], str]:
    category = ""
    cost = "unknown"
    domains: list[str] = []
    for tag in tags:
        key = tag.strip().lower()
        if not key:
            continue
        if key in _COSTS:
            cost = key
        elif key in _CATEGORIES and not category:
            category = tag.strip()
        elif key in _DOMAIN_LABELS or key not in _CATEGORIES:
            if key not in _COSTS and tag.strip() not in domains:
                domains.append(tag.strip())
    return category, domains, cost


def _is_meta_tag(value: str) -> bool:
    key = value.strip().lower()
    return key in _CATEGORIES or key in _COSTS or key in _DOMAIN_LABELS


def _tag_hit(tag: str, needle: str) -> bool:
    tag = tag.lower().strip()
    needle = needle.lower().strip()
    if tag == needle:
        return True
    if len(needle) <= 3:
        return re.search(rf"(?<![a-z]){re.escape(needle)}(?![a-z])", tag) is not None
    return needle in tag


def _search_blob(event: dict) -> str:
    return " ".join(
        [
            str(event.get("title") or ""),
            str(event.get("venue") or ""),
            str(event.get("city") or ""),
            str(event.get("summary") or ""),
            str(event.get("category") or ""),
            " ".join(event.get("domains") or []),
        ]
    ).lower()


def _sort_key(event: dict):
    start = event.get("start_date") or "9999-99-99"
    return (start, event.get("start_time") or "", event.get("title") or "")


def _merge(existing: list[dict], fresh: list[dict], scopes: list[dict]) -> list[dict]:
    def replaced(event: dict) -> bool:
        for scope in scopes:
            if event.get("source_id") != scope["source_id"]:
                continue
            cities = scope.get("cities")
            if not cities:
                return True
            if any(_same_city(event.get("city") or "", city) or (event.get("city") or "") == city for city in cities):
                return True
        return False

    kept = [event for event in existing if not replaced(event)]
    merged = kept + fresh
    merged.sort(key=_sort_key)
    return merged


def _dedupe(events: list[dict]) -> list[dict]:
    found = []
    seen = set()
    for event in events:
        key = event.get("id")
        if not key or key in seen:
            continue
        seen.add(key)
        found.append(event)
    return found


def _read_store(path: Path | None = None) -> dict:
    target = path or STORE_FILE
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"updated_at": None, "events": []}
    if not isinstance(payload, dict):
        return {"updated_at": None, "events": []}
    events = payload.get("events")
    if not isinstance(events, list):
        events = []
    return {"updated_at": payload.get("updated_at"), "events": events}


def _write_store(payload: dict, path: Path | None = None) -> None:
    target = path or STORE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def _fetch(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "EzyJobEvents/1.0",
            "Accept": "text/html,application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise EventsError(f"{url} returned {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise EventsError(f"{url} could not be reached ({exc.reason})") from exc
    return raw.decode("utf-8", "replace")


def _load_mysql() -> list[dict] | None:
    try:
        from jobscraper.db import get_conn, mysql_configured
    except Exception:
        return None
    if not mysql_configured():
        return None
    conn = get_conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT public_id, source_id, slug, title, city, region, venue, location, "
                "summary, category, domains, cost, date_label, when_label, start_date, end_date, "
                "start_time, end_time, url, website, source FROM tech_events"
            )
            rows = cur.fetchall() or []
    except Exception:
        log.warning("Could not read tech_events", exc_info=True)
        return None
    events = []
    for row in rows:
        domains = row.get("domains") or []
        if isinstance(domains, str):
            try:
                domains = json.loads(domains)
            except json.JSONDecodeError:
                domains = []
        events.append(
            {
                "id": row.get("public_id") or "",
                "slug": row.get("slug") or "",
                "title": row.get("title") or "",
                "city": row.get("city") or "",
                "region": row.get("region") or "",
                "venue": row.get("venue") or "",
                "location": row.get("location") or "",
                "summary": row.get("summary") or "",
                "category": row.get("category") or "",
                "domains": domains or [],
                "cost": row.get("cost") or "unknown",
                "date_label": row.get("date_label") or "",
                "when_label": row.get("when_label") or "",
                "start_date": _iso(row.get("start_date")) if row.get("start_date") else "",
                "end_date": _iso(row.get("end_date")) if row.get("end_date") else "",
                "start_time": _clock(row.get("start_time")),
                "end_time": _clock(row.get("end_time")),
                "url": row.get("url") or "",
                "website": row.get("website") or "",
                "source": row.get("source") or "",
                "source_id": row.get("source_id") or "",
            }
        )
    return events


def _save_mysql(events: list[dict], scopes: list[dict], scraped_at: str) -> None:
    try:
        from jobscraper.db import get_conn, mysql_configured
    except Exception:
        return
    if not mysql_configured():
        return
    conn = get_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_EVENTS_SQL)
            for scope in scopes:
                cities = scope.get("cities")
                if not cities:
                    cur.execute("DELETE FROM tech_events WHERE source_id=%s", (scope["source_id"],))
                else:
                    marks = ", ".join(["%s"] * len(cities))
                    cur.execute(
                        f"DELETE FROM tech_events WHERE source_id=%s AND city IN ({marks})",
                        (scope["source_id"], *cities),
                    )
            for event in events:
                if not any(event.get("source_id") == scope["source_id"] for scope in scopes):
                    continue
                cur.execute(
                    """
                    INSERT INTO tech_events (
                      public_id, source_id, slug, title, city, region, venue, location, summary,
                      category, domains, cost, date_label, when_label, start_date, end_date,
                      start_time, end_time, url, website, source, scraped_at
                    ) VALUES (
                      %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s
                    )
                    ON DUPLICATE KEY UPDATE
                      title=VALUES(title), city=VALUES(city), region=VALUES(region),
                      venue=VALUES(venue), location=VALUES(location), summary=VALUES(summary),
                      category=VALUES(category), domains=VALUES(domains), cost=VALUES(cost),
                      date_label=VALUES(date_label), when_label=VALUES(when_label),
                      start_date=VALUES(start_date), end_date=VALUES(end_date),
                      start_time=VALUES(start_time), end_time=VALUES(end_time),
                      url=VALUES(url), website=VALUES(website), source=VALUES(source),
                      scraped_at=VALUES(scraped_at)
                    """,
                    (
                        event["id"],
                        event["source_id"],
                        event["slug"],
                        event["title"],
                        event["city"],
                        event["region"],
                        event["venue"],
                        event["location"],
                        event["summary"],
                        event["category"],
                        json.dumps(event["domains"]),
                        event["cost"],
                        event["date_label"],
                        event["when_label"],
                        event["start_date"] or None,
                        event["end_date"] or None,
                        event["start_time"],
                        event["end_time"],
                        event["url"],
                        event["website"],
                        event["source"],
                        scraped_at.replace("T", " ").replace("+00:00", ""),
                    ),
                )
    except Exception:
        log.warning("Could not save tech_events", exc_info=True)


def _meta_text(article: str, pattern: str) -> str:
    match = re.search(pattern, article, re.S)
    return _clean(match.group(1)) if match else ""


def _split_place(meta: str) -> tuple[str, str]:
    parts = [part.strip() for part in re.split(r"\s·\s", meta) if part.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def _clean(fragment: str) -> str:
    text = re.sub(r"<!--.*?-->", " ", fragment or "", flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_dash(value: str) -> str:
    return (value or "").replace("–", "-").replace("—", "-").replace("−", "-")


def _parse_clock(value: str) -> tuple[str, str]:
    text = _normalize_dash(value)
    ranged = re.search(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", text)
    if ranged:
        return ranged.group(1), ranged.group(2)
    single = re.search(r"(\d{1,2}:\d{2})", text)
    if single:
        return single.group(1), ""
    return "", ""


def _make_date(day: int, month_name: str, year: int) -> date | None:
    month = _MONTHS.get(month_name.lower())
    if not month:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _iso(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return ""


def _clock(value) -> str:
    if value is None:
        return ""
    text = str(value)
    match = re.search(r"(\d{1,2}:\d{2})", text)
    return match.group(1) if match else ""


def _split_iso(value: str) -> tuple[str, str]:
    text = (value or "").strip()
    if not text:
        return "", ""
    if "T" in text:
        day, _, clock = text.partition("T")
        return day[:10], clock[:5] if re.match(r"\d{2}:\d{2}", clock) else ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text, ""
    return "", ""


def _absolute(href: str, page_url: str) -> str:
    href = (href or "").strip()
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        base = RTW_BASE if "riyadhtechweek.com" in page_url or page_url.startswith("/") else page_url
        if "sabtiah.cycls.com" in page_url:
            return "https://sabtiah.cycls.com" + href
        return RTW_BASE + href
    return href


def _external_link(article: str) -> str:
    for attrs, inner in _ANCHOR_RE.findall(article):
        href_match = _HREF_RE.search(attrs)
        if not href_match:
            continue
        href = html.unescape(href_match.group(1)).strip()
        if not href.startswith("http") or "riyadhtechweek.com" in href:
            continue
        label = _clean(inner).lower()
        if "event" in label or "site" in label or "website" in label:
            return href
    return ""


def _slug_from_url(url: str) -> str:
    path = urllib.request.urlparse(url).path.strip("/")
    if not path:
        return ""
    slug = path.rsplit("/", 1)[-1]
    return _slugify(slug)


def _slugify(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return text[:80] or "event"


def _error_text(exc: Exception) -> str:
    if isinstance(exc, EventsError):
        return str(exc)
    return str(exc.reason) if isinstance(exc, urllib.error.URLError) else str(exc)
