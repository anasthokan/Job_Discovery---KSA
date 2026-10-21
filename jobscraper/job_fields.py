"""Fields the Explore Jobs filters need, inferred from a listing.

Boards rarely send experience, visa, or Saudization as columns. Title, location,
and description are enough to fill the values the dashboard already shows:

  work_model                 remote | hybrid | onsite | unknown
  job_type                   fulltime | parttime | contract | internship
  experience_level           Intern/New Grad | Entry Level | Mid Level | Senior Level
                             | Lead/Staff | Director/Executive | unknown
  years_experience           minimum years stated in the posting, or null
  visa_sponsorship           yes | no | unknown   (Iqama / work visa)
  clearance_required         yes | no | unknown
  saudi_national_required    yes | no
"""

from __future__ import annotations

import re

WORK_MODELS = ("remote", "hybrid", "onsite")
JOB_TYPES = ("fulltime", "parttime", "contract", "internship")
EXPERIENCE_LEVELS = (
    "Intern/New Grad",
    "Entry Level",
    "Mid Level",
    "Senior Level",
    "Lead/Staff",
    "Director/Executive",
)
YES_NO = ("yes", "no")

_PLACEHOLDER_LOCS = {
    "",
    "saudi arabia",
    "ksa",
    "kingdom of saudi arabia",
    "anywhere",
    "n/a",
    "na",
    "unknown",
    "remote",
}

_HYBRID = re.compile(r"\bhybrid\b", re.I)
_REMOTE = re.compile(r"\b(remote|work from home|work-from-home|\bwfh\b)\b", re.I)
_NOT_REMOTE = re.compile(r"\b(no|not|non)[\s-]+remote\b", re.I)
_ONSITE_WORD = re.compile(r"\b(on[\s-]?site|in[\s-]?office)\b", re.I)
_REMOTE_PHRASE = re.compile(
    r"\b(fully remote|100% remote|remote position|remote role|remote-first|"
    r"this (?:is|role is) remote|work remotely)\b",
    re.I,
)

_TITLE_LEVELS = (
    (
        "Director/Executive",
        re.compile(
            r"\b(director|vice[\s-]?president|\bvp\b|head of|\bchief\b|"
            r"\bcto\b|\bceo\b|\bcfo\b|\bcoo\b|\bciso\b|\bexecutive\b)\b",
            re.I,
        ),
    ),
    (
        "Lead/Staff",
        re.compile(r"\b(staff|principal|distinguished|tech lead|team lead|\blead\b)\b", re.I),
    ),
    (
        "Intern/New Grad",
        re.compile(r"\b(intern(?:ship|s)?|new grad|new graduate)\b", re.I),
    ),
    ("Senior Level", re.compile(r"\b(senior|sr\.?)\b", re.I)),
    ("Entry Level", re.compile(r"\b(junior|jr\.?|entry[\s-]?level)\b", re.I)),
    ("Mid Level", re.compile(r"\b(mid[\s-]?level|intermediate)\b", re.I)),
)

_SENIORITY_LINE = re.compile(r"seniority(?: level)?\s*[:\-]\s*([^\n.]{3,40})", re.I)
_SENIORITY_MAP = (
    ("intern", "Intern/New Grad"),
    ("entry", "Entry Level"),
    ("associate", "Entry Level"),
    ("mid-senior", "Senior Level"),
    ("mid senior", "Senior Level"),
    ("director", "Director/Executive"),
    ("executive", "Director/Executive"),
    ("senior", "Senior Level"),
)

_YEARS = re.compile(
    r"(?:"
    r"(?:at least|minimum(?: of)?|min\.?)\s+(\d{1,2})\s*\+?\s*years"
    r"|(\d{1,2})\s*\+\s*years"
    r"|(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*\+?\s*years"
    r"|(\d{1,2})\s+years(?:'|\s)+(?:of\s+)?(?:relevant\s+|professional\s+|work\s+|industry\s+|hands-on\s+)?"
    r"(?:experience|exp\b)"
    r")",
    re.I,
)
_NO_EXPERIENCE = re.compile(
    r"\bno (?:prior |previous )?experience (?:required|needed|necessary)\b",
    re.I,
)

_VISA_NO = re.compile(
    r"saudi nationals? only|saudis only|only saudi|for saudi nationals?"
    r"|سعوديون فقط|للسعوديين فقط|لا نكفل"
    r"|(?:\bno\b|\bnot\b|\bwithout\b).{0,40}(?:\biqama\b|\bwork visa\b|\bsponsor(?:ship)?\b)"
    r"|\bno visa sponsorship\b"
    r"|\bsponsorship (?:is )?not (?:available|offered)\b",
    re.I,
)
_VISA_YES = re.compile(
    r"\biqama\b|\bwork visa\b|\bvisa sponsorship\b"
    r"|\bsponsorship (?:is )?available\b|\bwill sponsor\b|\bwe sponsor\b"
    r"|نقل كفالة|كفالة|تأشيرة عمل",
    re.I,
)

_CLEARANCE_NO = re.compile(
    r"\b(?:no|not|without|non)\b.{0,24}\bclearance\b"
    r"|\bclearance (?:is )?not required\b",
    re.I,
)
_CLEARANCE_YES = re.compile(
    r"\bsecurity clearance\b|\bclearance required\b"
    r"|\bmust (?:have|hold|possess)(?: a)? (?:security )?clearance\b"
    r"|تصريح أمني",
    re.I,
)

_SAUDI_ONLY = re.compile(
    r"saudi nationals? only|saudis only|only saudi nationals?"
    r"|saudi nationality (?:is )?(?:required|mandatory|a must)"
    r"|must be (?:a )?saudi(?: national)?"
    r"|سعوديون فقط|للسعوديين فقط|الجنسية السعودية",
    re.I,
)

_JOB_TYPE_ALIASES = {
    "full-time": "fulltime",
    "full time": "fulltime",
    "fulltime": "fulltime",
    "part-time": "parttime",
    "part time": "parttime",
    "parttime": "parttime",
    "contract": "contract",
    "contractor": "contract",
    "freelance": "contract",
    "temporary": "contract",
    "internship": "internship",
    "intern": "internship",
}
_WORK_ALIASES = {
    "remote": "remote",
    "wfh": "remote",
    "work from home": "remote",
    "hybrid": "hybrid",
    "onsite": "onsite",
    "on-site": "onsite",
    "on site": "onsite",
    "in-office": "onsite",
    "in office": "onsite",
}
_LEVEL_ALIASES = {label.lower(): label for label in EXPERIENCE_LEVELS}
_LEVEL_ALIASES.update(
    {
        "intern": "Intern/New Grad",
        "new grad": "Intern/New Grad",
        "entry": "Entry Level",
        "mid": "Mid Level",
        "senior": "Senior Level",
        "lead": "Lead/Staff",
        "staff": "Lead/Staff",
        "director": "Director/Executive",
        "executive": "Director/Executive",
    }
)


def _clip_text(*parts: object, limit: int = 20000) -> str:
    text = " ".join(str(part or "") for part in parts)
    return text[:limit]


def normalize_job_type(*values: object, description: str = "") -> str:
    """fulltime, parttime, contract, or internship. Unknown stays fulltime."""
    found = _explicit_job_type(" ".join(str(value or "") for value in values))
    if found:
        return found
    desc = description or ""
    if re.search(r"\bintern(?:ship|s)?\b", desc, re.I):
        return "internship"
    if re.search(r"part[\s_-]?time", desc, re.I):
        return "parttime"
    return "fulltime"


def _explicit_job_type(text: str) -> str | None:
    if not text.strip():
        return None
    if re.search(r"\bintern(?:ship|s)?\b", text, re.I):
        return "internship"
    if re.search(r"part[\s_-]?time", text, re.I):
        return "parttime"
    if re.search(r"\b(contract|contractor|freelance|temporary)\b", text, re.I):
        return "contract"
    if re.search(r"full[\s_-]?time", text, re.I):
        return "fulltime"
    return None


def infer_work_model(location: str = "", state: str = "", title: str = "", description: str = "") -> str:
    head = f"{location or ''} {state or ''} {title or ''}"
    if _HYBRID.search(head):
        return "hybrid"
    if _REMOTE.search(head) and not _NOT_REMOTE.search(head):
        return "remote"
    if (state or "").strip().lower() == "remote":
        return "remote"
    if _ONSITE_WORD.search(head):
        return "onsite"
    short = (description or "")[:2500]
    if _HYBRID.search(short):
        return "hybrid"
    if _REMOTE_PHRASE.search(short) and not _NOT_REMOTE.search(short):
        return "remote"
    place = (location or "").strip().lower()
    if place and place not in _PLACEHOLDER_LOCS:
        return "onsite"
    return "unknown"


def infer_years(title: str = "", description: str = "") -> int | None:
    text = _clip_text(title, description, limit=12000)
    found: list[int] = []
    for match in _YEARS.finditer(text):
        for group in match.groups():
            if not group:
                continue
            value = int(group)
            if 0 <= value <= 25:
                found.append(value)
    if found:
        return min(found)
    if _NO_EXPERIENCE.search(text):
        return 0
    return None


def infer_experience_level(
    title: str = "",
    description: str = "",
    job_type: str = "",
    years: int | None = None,
) -> str:
    for label, pattern in _TITLE_LEVELS:
        if pattern.search(title or ""):
            return label
    seniority = _SENIORITY_LINE.search((description or "")[:4000])
    if seniority:
        line = seniority.group(1).lower()
        for token, label in _SENIORITY_MAP:
            if token in line:
                return label
    if job_type == "internship":
        return "Intern/New Grad"
    if years is None:
        return "unknown"
    if years <= 1:
        return "Entry Level"
    if years <= 5:
        return "Mid Level"
    if years <= 9:
        return "Senior Level"
    return "Lead/Staff"


def _yes_no(text: str, no_re: re.Pattern[str], yes_re: re.Pattern[str]) -> str:
    """Negation only counts inside the same sentence, so 'No experience. Visa sponsorship' stays yes."""
    saw_no = False
    saw_yes = False
    for sentence in re.split(r"[\n.;]+", text):
        if no_re.search(sentence):
            saw_no = True
        elif yes_re.search(sentence):
            saw_yes = True
    if saw_yes and not saw_no:
        return "yes"
    if saw_no and not saw_yes:
        return "no"
    return "unknown"


def infer_visa(title: str = "", description: str = "") -> str:
    """Iqama or new work-visa sponsorship. unknown when the posting is silent."""
    return _yes_no(_clip_text(title, description), _VISA_NO, _VISA_YES)


def infer_clearance(title: str = "", description: str = "") -> str:
    return _yes_no(_clip_text(title, description), _CLEARANCE_NO, _CLEARANCE_YES)


def infer_saudi_national(title: str = "", description: str = "") -> str:
    """yes only when the posting is limited to Saudi nationals."""
    if _SAUDI_ONLY.search(_clip_text(title, description)):
        return "yes"
    return "no"


def infer_listing_fields(
    *,
    title: str = "",
    location: str = "",
    state: str = "",
    description: str = "",
    job_type: object = "",
) -> dict:
    kind = normalize_job_type(job_type, title, description=description or "")
    years = infer_years(title or "", description or "")
    return {
        "job_type": kind,
        "work_model": infer_work_model(location or "", state or "", title or "", description or ""),
        "experience_level": infer_experience_level(title or "", description or "", kind, years),
        "years_experience": years,
        "visa_sponsorship": infer_visa(title or "", description or ""),
        "clearance_required": infer_clearance(title or "", description or ""),
        "saudi_national_required": infer_saudi_national(title or "", description or ""),
    }


def _split(raw: str) -> list[str]:
    return [part.strip().lower() for part in (raw or "").split(",") if part.strip()]


def apply_listing_filters(
    jobs: list[dict],
    *,
    work_model: str = "",
    job_type: str = "",
    experience_level: str = "",
    visa_sponsorship: str = "",
    clearance_required: str = "",
    saudi_national_required: str = "",
    years: int | None = None,
) -> list[dict]:
    """Keep listings that match the Explore Jobs dropdowns. Blank filters are ignored."""
    work = {_WORK_ALIASES.get(part, part) for part in _split(work_model)}
    types = {_JOB_TYPE_ALIASES.get(part, part) for part in _split(job_type)}
    levels = {_LEVEL_ALIASES.get(part, part) for part in _split(experience_level)}
    visa = set(_split(visa_sponsorship))
    clearance = set(_split(clearance_required))
    saudi = set(_split(saudi_national_required))
    if not any((work, types, levels, visa, clearance, saudi)) and years is None:
        return jobs
    kept = []
    for job in jobs:
        if work and (job.get("work_model") or "unknown") not in work:
            continue
        if types and (job.get("job_type") or "fulltime") not in types:
            continue
        if levels and (job.get("experience_level") or "unknown") not in levels:
            continue
        if visa and (job.get("visa_sponsorship") or "unknown") not in visa:
            continue
        if clearance and (job.get("clearance_required") or "unknown") not in clearance:
            continue
        if saudi and (job.get("saudi_national_required") or "no") not in saudi:
            continue
        if years is not None:
            required = job.get("years_experience")
            if required is not None and int(required) > int(years):
                continue
        kept.append(job)
    return kept
