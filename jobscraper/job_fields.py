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
  compensation               salary text, or "salary not disclosed"
"""

from __future__ import annotations

import html
import json
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


SALARY_NOT_DISCLOSED = "salary not disclosed"

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_CUR = (
    r"(?:SAR|S\.?\s*R\.?|USD|US\$|AED|QAR|KWD|BHD|OMR|GBP|EUR|£|€|\$|"
    r"ريال(?:\s*سعودي)?|﷼|ر\.?\s*س)"
)
_NUM = (
    r"(?:\d{1,3}(?:[, ]\d{3})+(?:\.\d{1,2})?|\d{1,3}(?:\.\d)?\s*[kK]|"
    r"\d{4,7}(?:\.\d{1,2})?)(?!\d)"
)
_SEP = r"(?:–|—|-|to|and|upto|up\s+to|الى|إلى)"
_PER = (
    r"(?:(?:/|per|a|an)\s*)?(?:hours?|hrs?|hourly|days?|daily|weeks?|weekly|"
    r"months?|monthly|years?|yearly|annual(?:ly)?|annum|"
    r"لكل\s+شهر|في\s+الشهر|بالشهر|شهرياً|شهريا|شهري|سنوياً|سنويا|سنوي|بالساعة)"
)
_HOURLY_PAY = re.compile(
    rf"(?<![\d.,])(?P<c1>{_CUR})\s*(?P<n1>\d{{2,3}}(?:\.\d{{1,2}})?)"
    rf"(?:\s*{_SEP}\s*(?P<c3>{_CUR})?\s*(?P<n2>\d{{2,3}}(?:\.\d{{1,2}})?))?"
    rf"\s*(?P<per>(?:/|per)\s*(?:hours?|hrs?|hourly))",
    re.I,
)
_CURRENCY_PAY = re.compile(
    rf"(?<![\d.,])(?P<c1>{_CUR})\s*(?P<n1>{_NUM})(?:\s*(?P<c2>{_CUR}))?"
    rf"(?:\s*{_SEP}\s*(?P<c3>{_CUR})?\s*(?P<n2>{_NUM})(?:\s*(?P<c4>{_CUR}))?)?"
    rf"(?:\s*(?P<per>{_PER}))?",
    re.I,
)
_LOOSE_PAY = re.compile(
    rf"(?<![\d.,])(?P<c1>{_CUR})?\s*(?P<n1>{_NUM})(?:\s*(?P<c2>{_CUR}))?"
    rf"(?:\s*{_SEP}\s*(?P<c3>{_CUR})?\s*(?P<n2>{_NUM})(?:\s*(?P<c4>{_CUR}))?)?"
    rf"(?:\s*(?P<per>{_PER}))?",
    re.I,
)
_SALARY_CUE = re.compile(
    r"\b(?:salary|compensation|remuneration|stipend|wage|ctc|pay\s+range|basic\s+salary)\b"
    r"|راتب|الراتب|الأجر|الاجر",
    re.I,
)
_YEARS_AFTER = re.compile(r"^\s*(?:years|yrs|year)\b", re.I)
_MIN_KEYS = (
    "min_amount",
    "min",
    "minimum",
    "minValue",
    "min_value",
    "salaryMin",
    "minSalary",
    "salary_min",
)
_MAX_KEYS = (
    "max_amount",
    "max",
    "maximum",
    "maxValue",
    "max_value",
    "salaryMax",
    "maxSalary",
    "salary_max",
)
_CUR_KEYS = ("currency", "currency_type", "currencyCode", "currency_code", "salaryCurrency")
_PER_KEYS = ("interval", "salary_interval", "period", "unitText", "unit", "frequency")
_SALARY_OBJECTS = (
    "pay",
    "salary",
    "salaryRange",
    "salary_range",
    "baseSalary",
    "compensation",
    "remuneration",
    "pay_input_ranges",
    "pay_ranges",
)


def _blank_pay(value) -> bool:
    if value is None or value == "" or value == [] or value == {}:
        return True
    if isinstance(value, float) and value != value:
        return True
    if isinstance(value, str) and value.strip().lower() in {"", "none", "null", "nan", "nat"}:
        return True
    return False


def _money(value) -> float | None:
    if _blank_pay(value) or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().translate(_AR_DIGITS)
        text = text.replace(",", "").replace(" ", "").replace("٬", "")
        if text[:1] in "$£€":
            text = text[1:]
        scale = 1.0
        if text.lower().endswith("k"):
            scale = 1000.0
            text = text[:-1]
        text = re.sub(r"[^0-9.]", "", text)
        if not text:
            return None
        try:
            number = float(text) * scale
        except ValueError:
            return None
    if number <= 0 or number > 20_000_000:
        return None
    return number


def _fmt_amount(number: float) -> str:
    if abs(number - round(number)) < 0.001:
        return f"{int(round(number)):,}"
    return f"{number:,.2f}"


def _currency_code(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    compact = re.sub(r"[\s.]", "", raw).upper()
    if "ريال" in raw or "﷼" in raw or compact in {"SR", "RS", "SAR"}:
        return "SAR"
    if compact in {"$", "USD", "US$"} or raw.strip() in {"$", "US$"}:
        return "USD"
    if compact in {"€", "EUR"} or "€" in raw:
        return "EUR"
    if compact in {"£", "GBP"} or "£" in raw:
        return "GBP"
    if compact in {"AED", "QAR", "KWD", "BHD", "OMR"}:
        return compact
    if re.fullmatch(r"[A-Z]{3}", compact):
        return compact
    return ""


def _period(value) -> str:
    text = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    if not text:
        return ""
    if "hour" in text or "ساعة" in text or re.search(r"\bhr\b", text):
        return "hour"
    if "day" in text or "daily" in text:
        return "day"
    if "week" in text:
        return "week"
    if "month" in text or "شهري" in text or "شهر" in text:
        return "month"
    if any(token in text for token in ("year", "annual", "annum", "سنوي")):
        return "year"
    return ""


def _format_amounts(low: float | None, high: float | None, currency, period) -> str:
    amounts = [item for item in (low, high) if item is not None]
    if not amounts:
        return ""
    if len(amounts) == 2 and amounts[0] > amounts[1]:
        amounts = [amounts[1], amounts[0]]
    if len(amounts) == 2 and abs(amounts[0] - amounts[1]) < 0.5:
        amounts = [amounts[0]]
    period_label = _period(period)
    kept = []
    for amount in amounts:
        if period_label == "hour" and amount > 10_000:
            continue
        if period_label == "month" and amount > 500_000:
            continue
        if amount > 5_000_000:
            continue
        kept.append(amount)
    if not kept:
        return ""
    body = " – ".join(_fmt_amount(amount) for amount in kept)
    code = _currency_code(currency)
    if code:
        body = f"{code} {body}"
    if period_label:
        body = f"{body} / {period_label}"
    return body[:255]


def _first_present(data: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in data and not _blank_pay(data.get(key)):
            return data.get(key)
    return None


def _loads_pay(value):
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "ignore")
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "{[":
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return value
    return value


def _from_salary_object(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    currency = _first_present(data, _CUR_KEYS)
    period = _first_present(data, _PER_KEYS)
    if not _blank_pay(data.get("min_cents")) or not _blank_pay(data.get("max_cents")):
        low = _money(data.get("min_cents"))
        high = _money(data.get("max_cents"))
        if low is not None:
            low = low / 100
        if high is not None:
            high = high / 100
        found = _format_amounts(low, high, currency, period)
        if found:
            return found
    nested = data.get("value")
    if isinstance(nested, dict):
        merged = dict(nested)
        if _blank_pay(merged.get("currency")) and currency:
            merged["currency"] = currency
        found = _from_salary_object(merged)
        if found:
            return found
    low = _money(_first_present(data, _MIN_KEYS))
    high = _money(_first_present(data, _MAX_KEYS))
    if low is None and not isinstance(nested, dict):
        low = _money(nested)
    found = _format_amounts(low, high, currency, period)
    if found:
        return found
    for key in ("salaryDescription", "text", "label", "display", "description"):
        if isinstance(data.get(key), str):
            found = _from_text(data[key])
            if found:
                return found
    return ""


def _from_any(value) -> str:
    value = _loads_pay(value)
    if isinstance(value, dict):
        return _from_salary_object(value)
    if isinstance(value, list):
        for item in value:
            found = _from_any(item)
            if found:
                return found
        return ""
    if isinstance(value, str):
        return _from_text(value)
    return ""


def _as_mapping(value) -> dict:
    value = _loads_pay(value)
    return value if isinstance(value, dict) else {}


def _structured_compensation(*sources: object) -> str:
    for source in sources:
        data = _as_mapping(source)
        if not data:
            continue
        if any(not _blank_pay(data.get(key)) for key in ("min_amount", "max_amount", "min_cents", "max_cents")):
            found = _from_salary_object(data)
            if found:
                return found
        for key in _SALARY_OBJECTS:
            if key not in data:
                continue
            found = _from_any(data.get(key))
            if found:
                return found
    return ""


def _normalize_pay_text(value: str) -> str:
    text = str(value or "").translate(_AR_DIGITS)
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ").replace("٬", ",").replace("،", ",").replace("٫", ".")
    return re.sub(r"\s+", " ", text).strip()


def _amount_token(token: str | None) -> tuple[float | None, bool]:
    raw = str(token or "").strip()
    if not raw or raw[:1] == "0":
        return None, False
    scaled = bool(re.search(r"[kK]\s*$", raw))
    bare = re.sub(r"[^\d]", "", raw.split(".", 1)[0])
    if (
        not scaled
        and "," not in raw
        and " " not in raw
        and re.fullmatch(r"\d{4}", bare)
        and 1900 <= int(bare) <= 2100
    ):
        return None, False
    number = _money(raw)
    if number is None:
        return None, scaled
    return number, scaled


def _match_compensation(match: re.Match[str], text: str, *, require_currency: bool) -> str:
    after = text[match.end() : match.end() + 16]
    if _YEARS_AFTER.match(after):
        return ""
    groups = match.groupdict()
    per_token = str(groups.get("per") or "").strip()
    if re.fullmatch(r"years?|yrs", per_token, re.I):
        return ""
    currencies = [groups.get(name) for name in ("c1", "c2", "c3", "c4")]
    currency = next((item for item in currencies if item), "")
    if require_currency and not currency:
        return ""
    low, low_k = _amount_token(groups.get("n1"))
    high, high_k = (None, False)
    if groups.get("n2"):
        high, high_k = _amount_token(groups.get("n2"))
    if low is None and high is None:
        return ""
    if not currency and not low_k and not high_k:
        amounts = [item for item in (low, high) if item is not None]
        if not amounts or max(amounts) < 1000:
            return ""
    period = per_token
    if not period:
        tail = text[match.end() : match.end() + 24]
        head = text[max(0, match.start() - 40) : match.start()]
        period = _period(tail) or _period(head)
    has_range = low is not None and high is not None
    before = text[max(0, match.start() - 80) : match.start()]
    has_cue = bool(_SALARY_CUE.search(before))
    if not has_range and not _period(period) and not has_cue:
        return ""
    return _format_amounts(low, high, currency, period)


def _best_pay(pattern: re.Pattern[str], text: str, *, require_currency: bool) -> str:
    best = ""
    best_score = -1
    for match in pattern.finditer(text):
        found = _match_compensation(match, text, require_currency=require_currency)
        if not found:
            continue
        score = 0
        if re.search(r"\b(?:SAR|USD|AED|EUR|GBP|QAR|KWD)\b|[£€$]|ريال", found):
            score += 4
        if "–" in found:
            score += 2
        if "/" in found:
            score += 2
        if score > best_score:
            best = found
            best_score = score
    return best


def _from_text(value: str) -> str:
    text = _normalize_pay_text(value)[:20000]
    if not text:
        return ""
    found = _best_pay(_CURRENCY_PAY, text, require_currency=True)
    if found:
        return found
    found = _best_pay(_HOURLY_PAY, text, require_currency=True)
    if found:
        return found
    for cue in _SALARY_CUE.finditer(text):
        window = text[cue.start() : cue.start() + 220]
        found = _best_pay(_LOOSE_PAY, window, require_currency=False)
        if found:
            return found
    return ""


def _description_bits(job: dict, description: str) -> str:
    parts = [description, job.get("description"), job.get("search_text")]
    raw = _as_mapping(job.get("raw"))
    stored = _as_mapping(job.get("raw_description"))
    for source in (raw, stored):
        for key in ("jobDescription", "description", "salaryDescription", "text"):
            if isinstance(source.get(key), str):
                parts.append(source[key])
    return "\n".join(str(part) for part in parts if isinstance(part, str) and part.strip())


def compensation_text(job: dict | None = None, description: str = "") -> str:
    """Salary line for a scraped job. Silent postings stay 'salary not disclosed'."""
    job = job or {}
    found = _structured_compensation(job, job.get("raw"), job.get("raw_description"))
    if not found:
        found = _from_text(_description_bits(job, description))
    if not found:
        existing = str(job.get("compensation") or "").strip()
        if existing and existing.casefold() != SALARY_NOT_DISCLOSED:
            found = _from_text(existing)
    return found or SALARY_NOT_DISCLOSED


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
