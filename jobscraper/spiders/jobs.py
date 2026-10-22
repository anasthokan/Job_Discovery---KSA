import asyncio
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import scrapy

from jobscraper.items import JobItem
from jobscraper.job_fields import infer_listing_fields
from jobscraper.jobspy_source import scrape as scrape_jobspy
from jobscraper.jobspy_source import sites_for_platform
from jobscraper.locations import muse_location, normalize_scraped_location, parse_state

HTML_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
SYNONYMS = {
    "developer": ("developer", "engineer", "dev"),
    "engineer": ("engineer", "developer", "dev"),
    "frontend": ("frontend", "front end"),
    "backend": ("backend", "back end"),
    "fullstack": ("fullstack", "full stack"),
}


def _source_json(value) -> str:
    from jobscraper.db import source_extras_json

    return source_extras_json(value)


def strip_html(value) -> str:
    text = HTML_RE.sub(" ", str(value or ""))
    return WS_RE.sub(" ", text).strip()


def normalize_text(value) -> str:
    return NON_ALNUM_RE.sub(" ", str(value or "").lower()).strip()


def _token_in_blob(word: str, blob: str) -> bool:
    padded = f" {blob} "
    for alt in SYNONYMS.get(word, (word,)):
        if f" {normalize_text(alt)} " in padded:
            return True
    return False


def matches_keywords(keywords, *parts) -> bool:
    if not keywords:
        return True
    blob = normalize_text(" ".join(str(part or "") for part in parts))
    for keyword in keywords:
        kw = normalize_text(keyword)
        if not kw:
            continue
        if kw in blob:
            return True
        words = kw.split()
        if words and all(_token_in_blob(word, blob) for word in words):
            return True
    return False


def days_ago(value) -> int:
    if value is None or value == "":
        return 0
    posted = None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        try:
            posted = datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return 0
    else:
        cleaned = str(value).strip().replace("Z", "+00:00")
        try:
            posted = datetime.fromisoformat(cleaned)
        except ValueError:
            try:
                posted = parsedate_to_datetime(cleaned)
            except (TypeError, ValueError, OverflowError):
                return 0
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - posted).days)


DEFAULT_SEARCH_QUERIES = (
    "software engineer",
    "data analyst",
    "accountant",
    "nurse",
    "sales",
    "human resources",
    "project manager",
    "customer service",
    "mechanical engineer",
    "teacher",
)

# Used when a scrape asks for 1,000 or more jobs. One short run only covers a few of these.
BROAD_SEARCH_QUERIES = (
    "software engineer",
    "data analyst",
    "accountant",
    "nurse",
    "sales executive",
    "human resources",
    "project manager",
    "customer service",
    "mechanical engineer",
    "teacher",
    "civil engineer",
    "electrical engineer",
    "marketing",
    "driver",
    "chef",
    "pharmacist",
    "doctor",
    "receptionist",
    "security officer",
    "warehouse",
    "procurement",
    "finance",
    "graphic designer",
    "java developer",
    "python developer",
    "network engineer",
    "quality engineer",
    "safety officer",
    "logistics",
    "administrator",
    "technician",
    "architect",
    "lawyer",
    "recruiter",
    "business analyst",
    "devops engineer",
    "SAP consultant",
    "cashier",
    "storekeeper",
    "sales manager",
)


class JobsSpider(scrapy.Spider):
    """Saudi Arabia listings from Indeed, LinkedIn, Google Jobs, and Bayt."""

    name = "jobs"

    def __init__(self, keywords="", location="All", city="", platform="All", target="0", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.keywords = [k.strip().lower() for k in keywords.split(",") if k.strip()]
        self.location = location or "All"
        self.city = city or ""
        self.platform = platform or "All"
        try:
            self.target = max(0, int(target or 0))
        except (TypeError, ValueError):
            self.target = 0

    def _search_queries(self) -> list[str]:
        if self.keywords:
            return list(self.keywords)
        return list(DEFAULT_SEARCH_QUERIES)

    def _search_location(self) -> str:
        hint = muse_location(self.city, self.location)
        if self.location in ("All", "", None) or hint in ("", "Remote"):
            return "Saudi Arabia"
        if "saudi" in hint.lower():
            return hint
        return f"{hint}, Saudi Arabia"

    def errback(self, failure):
        self.logger.warning("Source request failed: %s", failure.value)

    def _matches(self, title, company, location, skills, extra=""):
        return matches_keywords(
            self.keywords,
            title,
            company,
            location,
            " ".join(skills or []),
            strip_html(extra),
        )

    def _emit(self, **fields):
        raw = fields.pop("raw", None)
        extra = fields.pop("search_text", "")
        if not self._matches(
            fields.get("title"),
            fields.get("company"),
            fields.get("location"),
            fields.get("skills") or [],
            extra,
        ):
            return
        kept = normalize_scraped_location(
            fields.get("state") or "",
            fields.get("location") or "",
            fields.get("platform") or "",
            extra,
        )
        if not kept:
            return
        fields["state"], fields["location"] = kept
        item = JobItem()
        for key, value in fields.items():
            item[key] = value
        description = strip_html(extra)
        if description:
            item["description"] = description
        item["raw_description"] = _source_json(raw)
        inferred = infer_listing_fields(
            title=fields.get("title") or "",
            location=fields.get("location") or "",
            state=fields.get("state") or "",
            description=description,
            job_type=fields.get("job_type") or "",
        )
        for key, value in inferred.items():
            item[key] = value
        yield item

    async def start(self):
        sites = sites_for_platform(self.platform)
        if not sites:
            self.logger.warning("No KSA board for platform %s", self.platform)
            return
        queries = self._search_queries()
        where = self._search_location()
        remote = self.location == "Remote"
        if self.target >= 1000 and not self.keywords:
            queries = list(BROAD_SEARCH_QUERIES)
        if self.target >= 1000:
            max_terms = min(len(queries), 40)
            slots = max(len(sites) * max_terms, 1)
            results_wanted = min(100, max(50, (self.target * 3 + slots - 1) // slots))
            hours_old = 24 * 180
        else:
            single_board = self.platform != "All"
            max_terms = 4 if single_board else 3
            results_wanted = 40 if single_board else 20
            hours_old = 720
        try:
            rows = await asyncio.to_thread(
                scrape_jobspy,
                queries,
                where,
                sites,
                is_remote=remote,
                results_wanted=results_wanted,
                max_terms=max_terms,
                hours_old=hours_old,
            )
        except Exception:
            self.logger.exception("JobSpy scrape failed")
            return
        self.logger.info("JobSpy returned %s jobs from %s in %s", len(rows), ", ".join(sites), where)
        for row in rows:
            location = row.get("location") or ""
            for item in self._emit(
                id=row["id"],
                title=row["title"],
                company=row["company"],
                skills=row.get("skills") or [],
                state=parse_state(location),
                location=location,
                platform=row["platform"],
                daysAgo=days_ago(row.get("posted_at")),
                url=row["url"],
                posted_at=row.get("posted_at") or "",
                job_type=row.get("job_type"),
                search_text=row.get("search_text") or "",
                raw=row.get("raw"),
            ):
                yield item
