import asyncio
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

import scrapy

from jobscraper.agent_reach import (
    DICE_HEADERS,
    JINA_HEADERS,
    dice_description_from_html,
    dice_detail_url,
    dice_search_urls,
    linkedin_description_from_html,
    smartrecruiters_description,
    yc_description_from_html,
    hn_comments_url,
    hn_latest_thread_url,
    is_antibot,
    linkedin_search_urls,
    parse_dice_search_html,
    parse_hn_comment,
    parse_linkedin_markdown,
    parse_yc_markdown,
    posting_paragraph,
    yc_jobs_url,
)
from jobscraper.boards import (
    ASHBY_BOARDS,
    GREENHOUSE_BOARDS,
    LEVER_COMPANIES,
    REMOTECO_FEED,
    SMARTRECRUITERS_COMPANIES,
    WORKABLE_ACCOUNTS,
    WWR_FEEDS,
)
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
    """Source fields that are not our job columns, saved in jobs.raw_description."""
    from jobscraper.db import source_extras_json

    return source_extras_json(value)


def _feed_item_raw(item) -> dict:
    """Every element on an RSS item, including fields we do not map onto the jobs API."""
    raw = {}
    for child in item.xpath("./*"):
        name = (child.xpath("name()").get() or "").split(":")[-1].strip()
        if not name:
            continue
        text = " ".join(part.strip() for part in child.xpath(".//text()").getall() if part.strip())
        if text:
            raw[name] = text
    return raw


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
    """True if no keywords, or if ANY keyword appears (OR)."""
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


def as_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def split_company_title(raw: str) -> tuple[str, str]:
    text = (raw or "").strip()
    if ": " in text:
        company, title = text.split(": ", 1)
        return company.strip() or "Unknown", title.strip() or text
    return "Unknown", text


MUSE_CATEGORIES = (
    "Software Engineering",
    "Engineering",
    "Data Science",
    "IT",
    "Design",
    "UX",
    "Product",
    "Marketing",
    "Sales",
    "Customer Service",
    "Account Management",
    "HR",
    "Recruiting",
    "Finance",
    "Accounting",
    "Project Management",
    "Operations",
    "Legal",
    "Writing",
    "Education",
    "Data and Analytics",
    "Science and Biotech",
    "Healthcare",
    "Nursing",
    "Administrative",
)

# Used for LinkedIn/Dice when no keyword filter is set (all fields).
DEFAULT_SEARCH_QUERIES = (
    "software engineer",
    "data analyst",
    "product manager",
    "marketing manager",
    "sales representative",
    "registered nurse",
    "accountant",
    "human resources",
    "customer service",
    "project manager",
    "graphic designer",
    "operations manager",
    "teacher",
    "legal assistant",
    "warehouse associate",
)


class JobsSpider(scrapy.Spider):
    """Public job APIs + company ATS boards, filtered by keyword."""

    name = "jobs"

    def __init__(self, keywords="", location="All", city="", platform="All", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.keywords = [k.strip().lower() for k in keywords.split(",") if k.strip()]
        self.location = location or "All"
        self.city = city or ""
        self.platform = platform or "All"

    def _search_queries(self) -> list[str]:
        if self.keywords:
            return list(self.keywords)
        return list(DEFAULT_SEARCH_QUERIES)

    def _search_query(self) -> str:
        return " ".join(self.keywords) if self.keywords else "software engineer"

    def _search_location(self) -> str:
        hint = muse_location(self.city, self.location)
        if self.location in ("All", "", None) or hint in ("", "Remote"):
            return "United States"
        if "united states" in hint.lower():
            return hint
        return f"{hint}, United States"

    def _jina_request(self, url, callback):
        return scrapy.Request(
            url,
            callback=callback,
            errback=self.errback,
            headers=JINA_HEADERS,
            meta={"download_timeout": 40},
        )

    def _wanted(self, selected, name):
        return selected in ("All", name)

    def _json(self, response):
        try:
            return response.json()
        except ValueError:
            self.logger.warning("Non-JSON response from %s", response.url)
            return {}

    async def start(self):
        headers = {"Accept": "application/json"}
        rss_headers = {"Accept": "application/rss+xml, application/xml, text/xml"}
        selected = self.platform
        city_or_state = muse_location(self.city, self.location)

        if self._wanted(selected, "RemoteOK"):
            yield scrapy.Request(
                "https://remoteok.com/api",
                callback=self.parse_remoteok,
                errback=self.errback,
                headers=headers,
            )

        if self._wanted(selected, "Remotive"):
            yield scrapy.Request(
                "https://remotive.com/api/remote-jobs",
                callback=self.parse_remotive,
                errback=self.errback,
                headers=headers,
            )

        if self._wanted(selected, "Jobicy"):
            yield scrapy.Request(
                "https://jobicy.com/api/v2/remote-jobs?count=200",
                callback=self.parse_jobicy,
                errback=self.errback,
                headers=headers,
            )

        if self._wanted(selected, "The Muse"):
            for category in MUSE_CATEGORIES:
                for page in range(10):
                    params = {"page": str(page), "descending": "true", "category": category}
                    if city_or_state and city_or_state != "Remote":
                        params["location"] = city_or_state
                    yield scrapy.Request(
                        f"https://www.themuse.com/api/public/jobs?{urlencode(params)}",
                        callback=self.parse_muse,
                        errback=self.errback,
                        headers=headers,
                    )

        if self._wanted(selected, "Arbeitnow"):
            for page in range(1, 25):
                yield scrapy.Request(
                    f"https://www.arbeitnow.com/api/job-board-api?page={page}",
                    callback=self.parse_arbeitnow,
                    errback=self.errback,
                    headers=headers,
                )

        if self._wanted(selected, "Himalayas"):
            for offset in range(0, 1000, 100):
                yield scrapy.Request(
                    f"https://himalayas.app/jobs/api?limit=100&offset={offset}",
                    callback=self.parse_himalayas,
                    errback=self.errback,
                    headers=headers,
                )

        if self._wanted(selected, "We Work Remotely"):
            for feed in WWR_FEEDS:
                yield scrapy.Request(
                    feed,
                    callback=self.parse_wwr,
                    errback=self.errback,
                    headers=rss_headers,
                )

        if self._wanted(selected, "Remote.co"):
            yield scrapy.Request(
                REMOTECO_FEED,
                callback=self.parse_remoteco,
                errback=self.errback,
                headers=rss_headers,
            )

        if self._wanted(selected, "Greenhouse"):
            for board in GREENHOUSE_BOARDS:
                yield scrapy.Request(
                    f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true",
                    callback=self.parse_greenhouse,
                    errback=self.errback,
                    headers=headers,
                    cb_kwargs={"company": board},
                )

        if self._wanted(selected, "Lever"):
            for company in LEVER_COMPANIES:
                yield scrapy.Request(
                    f"https://api.lever.co/v0/postings/{company}?mode=json",
                    callback=self.parse_lever,
                    errback=self.errback,
                    headers=headers,
                    cb_kwargs={"company": company},
                )

        if self._wanted(selected, "Ashby"):
            for board in ASHBY_BOARDS:
                yield scrapy.Request(
                    f"https://api.ashbyhq.com/posting-api/job-board/{board}",
                    callback=self.parse_ashby,
                    errback=self.errback,
                    headers=headers,
                    cb_kwargs={"company": board},
                )

        if self._wanted(selected, "SmartRecruiters"):
            for company in SMARTRECRUITERS_COMPANIES:
                yield scrapy.Request(
                    f"https://api.smartrecruiters.com/v1/companies/{company}/postings",
                    callback=self.parse_smartrecruiters,
                    errback=self.errback,
                    headers=headers,
                    cb_kwargs={"company": company},
                )

        if self._wanted(selected, "Workable"):
            for account in WORKABLE_ACCOUNTS:
                yield scrapy.Request(
                    f"https://apply.workable.com/api/v1/widget/accounts/{account}",
                    callback=self.parse_workable,
                    errback=self.errback,
                    headers=headers,
                    cb_kwargs={"company": account},
                )

        queries = self._search_queries()
        where = self._search_location()
        remote = self.location == "Remote"
        # A few pages per search so a full run can pass 1,000 saved US jobs.
        linkedin_pages = None if self.keywords and len(self.keywords) <= 3 else 3
        dice_pages = None if self.keywords and len(self.keywords) <= 3 else 2

        if self._wanted(selected, "LinkedIn"):
            for query in queries:
                urls = linkedin_search_urls(query, where, remote)
                if linkedin_pages:
                    urls = urls[:linkedin_pages]
                for url in urls:
                    yield self._jina_request(url, self.parse_linkedin)

        if self._wanted(selected, "Dice"):
            for query in queries:
                urls = dice_search_urls(query, where)
                if dice_pages:
                    urls = urls[:dice_pages]
                for url in urls:
                    yield scrapy.Request(
                        url,
                        callback=self.parse_dice,
                        errback=self.errback,
                        headers=DICE_HEADERS,
                        meta={"download_timeout": 40},
                    )

        if self._wanted(selected, "Y Combinator"):
            yield self._jina_request(yc_jobs_url(), self.parse_yc)

        if self._wanted(selected, "Hacker News"):
            yield scrapy.Request(
                hn_latest_thread_url(),
                callback=self.parse_hn_thread,
                errback=self.errback,
                headers=headers,
            )

        jobspy_sites = sites_for_platform(selected)
        if jobspy_sites:
            single_board = selected != "All"
            try:
                rows = await asyncio.to_thread(
                    scrape_jobspy,
                    queries,
                    where,
                    jobspy_sites,
                    is_remote=remote,
                    results_wanted=80 if single_board else 40,
                    max_terms=8 if single_board else 6,
                )
            except Exception:
                self.logger.exception("JobSpy scrape failed")
                rows = []
            self.logger.info("JobSpy returned %s jobs from %s", len(rows), ", ".join(jobspy_sites))
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

    def parse_remoteok(self, response):
        payload = self._json(response)
        rows = payload if isinstance(payload, list) else []
        for row in rows:
            if not isinstance(row, dict) or not row.get("position"):
                continue
            location = row.get("location") or "Remote"
            posted = row.get("date") or ""
            yield from self._emit(
                id=f"remoteok-{row.get('id')}",
                title=row.get("position"),
                company=row.get("company") or "Unknown",
                skills=as_list(row.get("tags"))[:8],
                state=parse_state(location),
                location=location or "Remote",
                platform="RemoteOK",
                daysAgo=days_ago(posted),
                url=row.get("url") or row.get("apply_url") or "",
                posted_at=str(posted),
                search_text=row.get("description") or "",
                raw=row,
            )

    def parse_remotive(self, response):
        payload = self._json(response)
        for row in payload.get("jobs") or []:
            location = row.get("candidate_required_location") or "Remote"
            posted = row.get("publication_date") or ""
            yield from self._emit(
                id=f"remotive-{row.get('id')}",
                title=row.get("title"),
                company=(row.get("company_name") or "Unknown").strip(),
                skills=as_list(row.get("tags"))[:8],
                state=parse_state(location),
                location=location,
                platform="Remotive",
                daysAgo=days_ago(posted),
                url=row.get("url") or "",
                posted_at=str(posted),
                job_type=row.get("job_type"),
                search_text=row.get("description") or "",
                raw=row,
            )

    def parse_jobicy(self, response):
        payload = self._json(response)
        for row in payload.get("jobs") or []:
            location = row.get("jobGeo") or "Remote"
            posted = row.get("pubDate") or ""
            skills = as_list(row.get("jobIndustry")) + as_list(row.get("jobType"))
            yield from self._emit(
                id=f"jobicy-{row.get('id')}",
                title=row.get("jobTitle"),
                company=row.get("companyName") or "Unknown",
                skills=skills[:8],
                state=parse_state(location),
                location=location,
                platform="Jobicy",
                daysAgo=days_ago(posted),
                url=row.get("url") or "",
                posted_at=str(posted),
                job_type=row.get("jobType"),
                search_text=row.get("jobDescription") or row.get("jobExcerpt") or "",
                raw=row,
            )

    def parse_muse(self, response):
        payload = self._json(response)
        for row in payload.get("results") or []:
            locations = row.get("locations") or []
            location = ", ".join(loc.get("name", "") for loc in locations if loc.get("name")) or "Remote"
            posted = row.get("publication_date") or ""
            skills = as_list([c.get("name") for c in row.get("categories") or []])
            skills += as_list([t.get("name") for t in row.get("tags") or []])
            company = (row.get("company") or {}).get("name") or "Unknown"
            url = (row.get("refs") or {}).get("landing_page") or ""
            yield from self._emit(
                id=f"muse-{row.get('id')}",
                title=row.get("name"),
                company=company,
                skills=skills[:8],
                state=parse_state(location),
                location=location,
                platform="The Muse",
                daysAgo=days_ago(posted),
                url=url,
                posted_at=str(posted),
                search_text=row.get("contents") or "",
                raw=row,
            )

    def parse_arbeitnow(self, response):
        payload = self._json(response)
        for row in payload.get("data") or []:
            location = row.get("location") or ("Remote" if row.get("remote") else "")
            if row.get("remote") and "remote" not in location.lower():
                location = f"{location}, Remote" if location else "Remote"
            posted = row.get("created_at") or ""
            yield from self._emit(
                id=f"arbeitnow-{row.get('slug') or row.get('url')}",
                title=row.get("title"),
                company=row.get("company_name") or "Unknown",
                skills=as_list(row.get("tags"))[:8],
                state=parse_state(location),
                location=location or "Remote",
                platform="Arbeitnow",
                daysAgo=days_ago(posted),
                url=row.get("url") or "",
                posted_at=str(posted),
                job_type=" ".join(as_list(row.get("job_types"))),
                search_text=row.get("description") or "",
                raw=row,
            )

    def parse_himalayas(self, response):
        payload = self._json(response)
        for row in payload.get("jobs") or []:
            location = ", ".join(as_list(row.get("locationRestrictions"))) or "Remote"
            posted = row.get("pubDate") or ""
            yield from self._emit(
                id=f"himalayas-{row.get('guid') or row.get('title')}",
                title=row.get("title"),
                company=row.get("companyName") or "Unknown",
                skills=as_list(row.get("categories"))[:8],
                state=parse_state(location),
                location=location,
                platform="Himalayas",
                daysAgo=days_ago(posted),
                url=row.get("applicationLink") or row.get("guid") or "",
                posted_at=str(posted),
                job_type=row.get("employmentType"),
                search_text=f"{row.get('excerpt') or ''} {row.get('description') or ''}",
                raw=row,
            )

    def parse_wwr(self, response):
        for item in response.xpath("//item"):
            raw_title = " ".join(item.xpath("title//text()").getall()).strip()
            company, title = split_company_title(raw_title)
            link = " ".join(item.xpath("link//text()").getall()).strip()
            desc = strip_html(" ".join(item.xpath("description//text()").getall()))
            posted = " ".join(item.xpath("pubDate//text()").getall()).strip()
            location = "Remote"
            hay = f"{title} {desc}".lower()
            if "united states" in hay or "usa" in hay or "u.s." in hay:
                location = "Remote, United States"
            yield from self._emit(
                id=f"wwr-{link or title}",
                title=title,
                company=company,
                skills=[],
                state=parse_state(location),
                location=location,
                platform="We Work Remotely",
                daysAgo=days_ago(posted),
                url=link,
                posted_at=posted,
                search_text=desc,
                raw=_feed_item_raw(item),
            )

    def parse_remoteco(self, response):
        for item in response.xpath("//item"):
            raw_title = " ".join(item.xpath("title//text()").getall()).strip()
            company, title = split_company_title(raw_title)
            if title == raw_title:
                title = raw_title
            link = " ".join(item.xpath("link//text()").getall()).strip()
            desc = strip_html(" ".join(item.xpath("description//text()").getall()))
            posted = " ".join(item.xpath("pubDate//text()").getall()).strip()
            location = "Remote"
            yield from self._emit(
                id=f"remoteco-{link or title}",
                title=title,
                company=company,
                skills=[],
                state=parse_state(location),
                location=location,
                platform="Remote.co",
                daysAgo=days_ago(posted),
                url=link,
                posted_at=posted,
                search_text=desc,
                raw=_feed_item_raw(item),
            )

    def parse_greenhouse(self, response, company=""):
        payload = self._json(response)
        display = company.replace("-", " ").title()
        for row in payload.get("jobs") or []:
            location = ((row.get("location") or {}).get("name")) or "Remote"
            posted = row.get("updated_at") or row.get("first_published") or ""
            yield from self._emit(
                id=f"greenhouse-{company}-{row.get('id')}",
                title=row.get("title"),
                company=display,
                skills=[],
                state=parse_state(location),
                location=location,
                platform="Greenhouse",
                daysAgo=days_ago(posted),
                url=row.get("absolute_url") or "",
                posted_at=str(posted),
                search_text=row.get("content") or "",
                raw=row,
            )

    def parse_lever(self, response, company=""):
        payload = self._json(response)
        rows = payload if isinstance(payload, list) else []
        display = company.replace("-", " ").title()
        for row in rows:
            cats = row.get("categories") or {}
            location = cats.get("location") or cats.get("commitment") or "Remote"
            posted = row.get("createdAt") or ""
            yield from self._emit(
                id=f"lever-{company}-{row.get('id')}",
                title=row.get("text"),
                company=display,
                skills=as_list([cats.get("team"), cats.get("commitment")]),
                state=parse_state(location),
                location=location,
                platform="Lever",
                daysAgo=days_ago(posted),
                url=row.get("hostedUrl") or row.get("applyUrl") or "",
                posted_at=str(posted),
                job_type=cats.get("commitment"),
                search_text=str((row.get("descriptionPlain") or row.get("description") or "")),
                raw=row,
            )

    def parse_ashby(self, response, company=""):
        payload = self._json(response)
        display = company.replace("-", " ").title()
        for row in payload.get("jobs") or []:
            location = row.get("location") or "Remote"
            extras = []
            for loc in row.get("secondaryLocations") or []:
                if isinstance(loc, dict):
                    extras.append(loc.get("location") or "")
                else:
                    extras.append(str(loc))
            extra_text = ", ".join(part for part in extras if part)
            if extra_text:
                location = f"{location}, {extra_text}" if location else extra_text
            if row.get("isRemote") and "remote" not in (location or "").lower():
                location = f"{location}, Remote" if location else "Remote"
            posted = row.get("publishedAt") or row.get("updatedAt") or ""
            dept = row.get("department")
            yield from self._emit(
                id=f"ashby-{company}-{row.get('id')}",
                title=row.get("title"),
                company=display,
                skills=as_list(dept if isinstance(dept, list) else dept),
                state=parse_state(location),
                location=location or "Remote",
                platform="Ashby",
                daysAgo=days_ago(posted),
                url=row.get("jobUrl") or row.get("applyUrl") or "",
                posted_at=str(posted),
                job_type=row.get("employmentType"),
                search_text=row.get("descriptionPlain") or row.get("descriptionHtml") or "",
                raw=row,
            )

    def parse_smartrecruiters(self, response, company=""):
        payload = self._json(response)
        for row in payload.get("content") or []:
            loc = row.get("location") or {}
            parts = [loc.get("city") or "", loc.get("region") or "", loc.get("country") or ""]
            location = ", ".join(part for part in parts if part) or "Remote"
            posted = row.get("releasedDate") or row.get("createdOn") or ""
            ident = row.get("id") or row.get("uuid") or ""
            url = row.get("ref") or (f"https://jobs.smartrecruiters.com/{company}/{ident}" if ident else "")
            fn = row.get("function")
            skills = as_list(fn.get("label") if isinstance(fn, dict) else fn)
            if not ident:
                continue
            yield scrapy.Request(
                f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{ident}",
                callback=self.parse_smartrecruiters_detail,
                errback=self.errback,
                headers={"Accept": "application/json"},
                cb_kwargs={
                    "job": {
                        "id": f"smartrecruiters-{company}-{ident}",
                        "title": row.get("name"),
                        "company": company,
                        "skills": skills,
                        "location": location,
                        "url": url,
                        "posted_at": str(posted),
                        "raw": row,
                    }
                },
            )

    def parse_workable(self, response, company=""):
        payload = self._json(response)
        display = company.replace("-", " ").title()
        for row in payload.get("jobs") or []:
            locs = row.get("locations") or row.get("location") or []
            if isinstance(locs, dict):
                location = locs.get("city") or locs.get("country") or "Remote"
            elif isinstance(locs, list):
                names = []
                for loc in locs:
                    if isinstance(loc, dict):
                        names.append(loc.get("city") or loc.get("name") or loc.get("country") or "")
                    else:
                        names.append(str(loc))
                location = ", ".join(name for name in names if name) or "Remote"
            else:
                location = str(locs or "Remote")
            posted = row.get("created_at") or row.get("published_on") or ""
            shortcode = row.get("shortcode") or row.get("id") or ""
            url = row.get("url") or (f"https://apply.workable.com/{company}/j/{shortcode}/" if shortcode else "")
            yield from self._emit(
                id=f"workable-{company}-{shortcode}",
                title=row.get("title"),
                company=display,
                skills=as_list(row.get("department")),
                state=parse_state(location),
                location=location,
                platform="Workable",
                daysAgo=days_ago(posted),
                url=url,
                posted_at=str(posted),
                search_text=row.get("description") or "",
                raw=row,
            )

    def parse_linkedin(self, response):
        if is_antibot(response.text):
            self.logger.warning("LinkedIn Jina Reader returned an antibot page")
            return
        for job in parse_linkedin_markdown(response.text):
            match = re.search(r"(\d{6,})", job.get("url") or job.get("id") or "")
            if not match:
                continue
            yield scrapy.Request(
                f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{match.group(1)}",
                callback=self.parse_linkedin_detail,
                errback=self.errback,
                headers=DICE_HEADERS,
                cb_kwargs={"job": job},
                meta={"download_timeout": 25},
            )

    def _dice_items(self, job, description):
        source = dict(job.get("raw") or {})
        if description:
            source["jobDescription"] = description
        yield from self._emit(
            id=job["id"],
            title=job["title"],
            company=job["company"],
            skills=[],
            state=parse_state(job.get("location") or ""),
            location=job.get("location") or "",
            platform="Dice",
            daysAgo=days_ago(job.get("posted_at")),
            url=job["url"],
            posted_at=job.get("posted_at") or "",
            job_type=job.get("job_type") or "",
            search_text=description,
            raw=source or None,
        )

    def parse_dice(self, response):
        if is_antibot(response.text):
            self.logger.warning("Dice search returned an antibot page")
            return
        for job in parse_dice_search_html(response.text):
            yield scrapy.Request(
                dice_detail_url(job["url"]),
                callback=self.parse_dice_detail,
                errback=self.errback_dice,
                headers=DICE_HEADERS,
                cb_kwargs={"job": job},
                meta={"download_timeout": 25},
            )

    def parse_dice_detail(self, response, job):
        if is_antibot(response.text):
            description = posting_paragraph(job.get("summary") or "", job.get("title") or "")
        else:
            body = dice_description_from_html(response.text)
            summary = job.get("summary") or ""
            description = body if len(body) >= len(summary) else summary
            description = posting_paragraph(description, job.get("title") or "")
        yield from self._dice_items(job, description)

    def errback_dice(self, failure):
        request = getattr(failure, "request", None)
        job = (getattr(request, "cb_kwargs", None) or {}).get("job") if request else None
        if not job:
            return
        description = posting_paragraph(job.get("summary") or "", job.get("title") or "")
        yield from self._dice_items(job, description)

    def parse_smartrecruiters_detail(self, response, job):
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        description = smartrecruiters_description(payload)
        source = dict(job.get("raw") or {})
        source["posting"] = payload
        if description:
            source["jobDescription"] = description
        yield from self._emit(
            id=job["id"],
            title=job["title"],
            company=job["company"],
            skills=job.get("skills") or [],
            state=parse_state(job.get("location") or ""),
            location=job.get("location") or "",
            platform="SmartRecruiters",
            daysAgo=days_ago(job.get("posted_at")),
            url=job["url"],
            posted_at=job.get("posted_at") or "",
            search_text=description,
            raw=source,
        )

    def parse_linkedin_detail(self, response, job):
        description = "" if is_antibot(response.text) else linkedin_description_from_html(response.text)
        source = dict(job)
        if description:
            source["jobDescription"] = description
        yield from self._emit(
            id=job["id"],
            title=job["title"],
            company=job["company"],
            skills=[],
            state=parse_state(job.get("location") or ""),
            location=job.get("location") or "",
            platform=job.get("platform") or "LinkedIn",
            daysAgo=job.get("daysAgo") or 0,
            url=job["url"],
            posted_at=job.get("posted_at") or "",
            search_text=description,
            raw=source,
        )

    def parse_yc(self, response):
        if is_antibot(response.text):
            self.logger.warning("Y Combinator Jina Reader returned an antibot page")
            return
        for job in parse_yc_markdown(response.text):
            if not job.get("url"):
                continue
            yield scrapy.Request(
                job["url"],
                callback=self.parse_yc_detail,
                errback=self.errback,
                headers=DICE_HEADERS,
                cb_kwargs={"job": job},
                meta={"download_timeout": 25},
            )

    def parse_yc_detail(self, response, job):
        description = "" if is_antibot(response.text) else yc_description_from_html(response.text)
        source = dict(job)
        if description:
            source["jobDescription"] = description
        yield from self._emit(
            id=job["id"],
            title=job["title"],
            company=job["company"],
            skills=[],
            state=parse_state(job.get("location") or ""),
            location=job.get("location") or "",
            platform=job.get("platform") or "Y Combinator",
            daysAgo=job.get("daysAgo") or 0,
            url=job["url"],
            posted_at=job.get("posted_at") or "",
            search_text=description,
            raw=source,
        )

    def parse_hn_thread(self, response):
        payload = self._json(response)
        hits = payload.get("hits") or []
        if not hits:
            return
        story_id = str(hits[0].get("objectID") or "")
        if not story_id:
            return
        query = " ".join(self.keywords)
        for page in (0, 1):
            yield scrapy.Request(
                hn_comments_url(story_id, query, page),
                callback=self.parse_hn_comments,
                errback=self.errback,
                headers={"Accept": "application/json"},
            )

    def parse_hn_comments(self, response):
        payload = self._json(response)
        for hit in payload.get("hits") or []:
            job = parse_hn_comment(hit)
            if not job:
                continue
            yield from self._emit(
                id=job["id"],
                title=job["title"],
                company=job["company"],
                skills=[],
                state=parse_state(job["location"]),
                location=job["location"],
                platform=job["platform"],
                daysAgo=days_ago(job["posted_at"]),
                url=job["url"],
                posted_at=job["posted_at"],
                search_text=job.get("search_text") or "",
                raw=hit,
            )
