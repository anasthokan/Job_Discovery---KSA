"""Tailor a saved user profile to one job without inventing experience."""

from __future__ import annotations

import logging
import re
from typing import Any

from jobscraper.jd_to_cv import JobToCvError, build_cv_from_job, render_cv_text
from jobscraper.ollama_resume import OllamaError, ollama_enabled, rewrite_summary
from jobscraper.resume_parser import ResumeParseError, parse_resume_text

log = logging.getLogger("job-discovery")


_HEADING_LABELS = (
    "PROFESSIONAL SUMMARY",
    "CAREER OBJECTIVE",
    "KEY ACHIEVEMENTS",
    "PROFESSIONAL EXPERIENCE",
    "WORK EXPERIENCE",
    "WORK HISTORY",
    "CORE COMPETENCIES",
    "TECHNICAL SKILLS",
    "CORE SKILLS",
    "CERTIFICATIONS",
    "EDUCATION",
    "PROJECTS",
    "LANGUAGES",
    "SUMMARY",
    "EXPERIENCE",
    "SKILLS",
    "OBJECTIVE",
    "AWARDS",
    "PUBLICATIONS",
)
_HEADING_RE = re.compile(
    "(" + "|".join(re.escape(item) for item in sorted(_HEADING_LABELS, key=len, reverse=True)) + ")",
    re.I,
)
_HEADING_LINE_RE = re.compile(
    r"(?i)^(" + "|".join(re.escape(item) for item in _HEADING_LABELS) + r")$"
)
_UUID_RE = re.compile(r"\b[0-9a-f]{16,}\b", re.I)
_JOB_AD_RE = re.compile(
    r"(?is)^(.{0,500}?\$\s?\d[\d,\s\-–—]*\d?.{0,80}?(?:benefits|bonus)\s+)"
)
_DATE_RE = re.compile(
    r"(?i)\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}\b|\b(?:19|20)\d{2}\s*[-–—]\s*(?:present|current|now|(?:19|20)\d{2})\b"
)
_ROLE_RE = re.compile(
    r"(?i)\b(engineers?|developers?|managers?|analysts?|architects?|interns?|consultants?|specialists?|accountants?|registrars?|designers?|scientists?|administrators?)\b"
)


def enhance_profile_for_job(profile: dict[str, Any], job: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keep every original resume section, then aim that same content at the job."""
    base = _with_real_name(compose_resume_document(profile), profile)
    document, mode = _aim_document_at_job(base, job or {})
    if len(document.strip()) < 40:
        raise JobToCvError("This user has no resume or profile content to download.")
    resume = resume_from_profile({**profile, "resume_text": document, "resume_document": document})
    job = job or {}
    match = {
        "score": None,
        "matched_skills": [],
        "missing_skills": [],
        "extra_skills": [],
    }
    # Optional overlap report only. The file content stays the profile resume.
    if job.get("skills") or job.get("description") or job.get("title"):
        try:
            tailored = build_cv_from_job(
                str(job.get("description") or ""),
                job_title=job.get("title"),
                company=job.get("company"),
                location=job.get("location"),
                job_skills=list(job.get("skills") or []) or None,
                resume=resume,
                include_cv_text=False,
            )
            match = tailored.get("match") or match
        except JobToCvError:
            pass
    return {
        "ok": True,
        "mode": mode,
        "job": {
            "title": job.get("title"),
            "company": job.get("company"),
            "location": job.get("location"),
        },
        "match": match,
        "cv": resume,
        "cv_text": document,
    }


def compose_resume_document(profile: dict[str, Any]) -> str:
    """Use the uploaded/saved resume as-is. Rebuild from profile only when that text is incomplete."""
    preserved = preserved_resume_text(profile)
    sections = _original_sections(_profile_blob(profile))
    if len(preserved) >= 500 and (
        _heading_count(preserved) >= 2 or _has_named_sections(preserved) or len(sections) >= 2
    ):
        return _polish_document(preserved, profile, sections)
    rebuilt = rebuild_resume_from_profile(profile)
    # A longer original resume wins. Rebuilding must not shrink it.
    if len(preserved) > max(len(rebuilt), 500):
        return _polish_document(preserved, profile, sections)
    if _section_richness(rebuilt) > _section_richness(preserved):
        return _polish_document(rebuilt, profile, sections)
    return _polish_document(preserved or rebuilt, profile, sections)


def preserved_resume_text(profile: dict[str, Any]) -> str:
    """Resume file text when we have it, otherwise the saved resume with job-ad junk removed."""
    file_text = str(profile.get("resume_document") or "").strip()
    saved = str(profile.get("resume_text") or "").strip()
    summary = str(profile.get("professional_summary") or "").strip()
    # Full uploaded resume first. Light-clean only when job-board junk is mixed in.
    candidates = []
    for raw in (file_text, saved, summary):
        if not raw:
            continue
        body = _restore_resume(raw) if _needs_resume_cleanup(raw) else _light_normalize(raw)
        if body:
            candidates.append(body)
    ranked = sorted(
        candidates,
        key=lambda item: (_heading_count(item), _has_named_sections(item), len(item)),
        reverse=True,
    )
    return ranked[0] if ranked else ""


def rebuild_resume_from_profile(profile: dict[str, Any]) -> str:
    """Build a sectioned resume from saved profile fields when the uploaded file is missing."""
    source = dict(profile)
    raw_blob = "\n".join(
        part
        for part in (
            str(profile.get("resume_document") or "").strip(),
            str(profile.get("resume_text") or "").strip(),
            str(profile.get("professional_summary") or "").strip(),
            str(profile.get("headline") or "").strip(),
        )
        if part
    )
    blob = _restore_resume(raw_blob) if _needs_resume_cleanup(raw_blob) else _light_normalize(raw_blob)
    if len(blob) >= 40:
        source["resume_text"] = blob
    # Keep only skills that actually appear in the cleaned resume/profile text.
    source["skills"] = _skills_grounded_in_text(profile.get("skills") or [], blob)
    resume = resume_from_profile(source)
    summary = str(profile.get("professional_summary") or "").strip()
    if summary and not (resume.get("profile") or {}).get("summary"):
        resume.setdefault("profile", {})["summary"] = summary
    if not _has_content(resume):
        return ""
    text = render_cv_text(resume)
    return _normalize_section_titles(text)


def _skills_grounded_in_text(skills: list[str], text: str) -> list[str]:
    blob = (text or "").lower()
    if len(blob) < 40:
        return []
    kept = []
    for skill in skills:
        name = str(skill or "").strip()
        if not name or _is_junk_line(name):
            continue
        if name.lower() in blob:
            kept.append(name)
    return kept


def _section_richness(text: str) -> tuple[int, int, int]:
    return (_heading_count(text), 1 if _has_named_sections(text) else 0, len(text or ""))


def _needs_resume_cleanup(text: str) -> bool:
    lowered = text.lower()
    if "$" in text and "bonus" in lowered:
        return True
    if _PLAN_CREDITS_RE.search(lowered) or "mid-senior" in lowered:
        return True
    if _UUID_RE.search(text):
        return True
    title_lines = sum(
        1
        for line in text.splitlines()
        if _is_posting_title(re.sub(r"[ \t]{2,}", " ", line).strip())
    )
    return title_lines >= 3


def _light_normalize(text: str) -> str:
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    cleaned = cleaned.replace("\u2022", "- ").replace("•", "- ")
    cleaned = _separate_headings(cleaned)
    lines = [re.sub(r"[ \t]{2,}", " ", line).rstrip() for line in cleaned.splitlines()]
    lines = [line for line in lines if line.strip()]
    return "\n".join(lines).strip() + ("\n" if lines else "")


_PLAN_CREDITS_RE = re.compile(
    r"(?i)\b(?:free|pro|basic|premium|business)\s+plan\s+monthly\s+credits\b\.?"
)
_ISSUER_ONLY_RE = re.compile(
    r"(?i)^(?:project management institute(?:\s*\(pmi\))?|axelos|iiba|pmi)$"
)
_COMPANY_LEAD_RE = re.compile(r"^([A-Z][A-Za-z0-9&]{2,40})\s+is\b")


_TAILOR_STOP = {
    "the", "and", "for", "with", "you", "our", "this", "that", "from", "your",
    "role", "job", "team", "work", "using", "have", "will", "are", "was",
    "senior", "junior", "level", "years", "year", "experience",
}


def resume_text_rank(text: str) -> tuple[int, int, int]:
    """Richer resumes sort first: more section headings, then named sections, then length."""
    return (_heading_count(text), 1 if _has_named_sections(text) else 0, len(text or ""))


def _aim_document_at_job(document: str, job: dict[str, Any]) -> tuple[str, str]:
    """Keep the original resume and replace only the summary with a job-targeted paragraph."""
    summary = _summary_from_ollama(document, job)
    if summary:
        return _replace_summary_section(document, summary), "ollama"
    return tailor_document_for_job(document, job), "profile"


def _summary_from_ollama(document: str, job: dict[str, Any]) -> str:
    if not ollama_enabled() or len((document or "").strip()) < 40 or not job:
        return ""
    if not (str(job.get("title") or "").strip() or str(job.get("description") or "").strip()):
        return ""
    try:
        summary = rewrite_summary(document, job)
    except OllamaError as exc:
        log.warning("Ollama resume enhance skipped: %s", exc)
        return ""
    except Exception as exc:
        log.warning("Ollama resume enhance failed: %s", exc)
        return ""
    if not _summary_is_grounded(summary, document, job):
        log.warning("Ollama summary was rejected; keeping the original resume sections")
        return ""
    return summary


_SUMMARY_HEADINGS = {"PROFESSIONAL SUMMARY", "SUMMARY", "CAREER OBJECTIVE", "OBJECTIVE"}
_NAME_BLOCK = {
    "plan", "credits", "monthly", "free", "pro", "profile", "resume", "cv",
    "team", "lead", "leader", "manager", "head", "senior", "junior", "director",
    "engineer", "product", "specialist", "officer", "consultant", "analyst",
    "intern", "developer", "architect", "riyadh", "jeddah", "dammam", "khobar",
}


def _with_real_name(document: str, profile: dict[str, Any]) -> str:
    """Put the person's name and headline back when a job title was used as the name."""
    lines = [line for line in (document or "").splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return document
    name = (
        _usable_person_name(_join_name(profile.get("first_name"), profile.get("last_name")) or "")
        or _name_from_text(document)
    )
    if name and not _usable_person_name(lines[0]):
        if _source_contact_line(lines[0]) or _HEADING_LINE_RE.match(lines[0].strip()):
            lines.insert(0, name)
        else:
            lines[0] = name
    headline = _usable_headline(str(profile.get("headline") or ""))
    opening = "\n".join(lines[:8]).lower()
    if headline and headline.lower() not in opening:
        lines.insert(1, headline)
    return "\n".join(lines).strip() + "\n"


def _replace_summary_section(document: str, summary: str) -> str:
    paragraph = re.sub(r"\s+", " ", summary).strip()
    pieces = _split_resume_sections(document)
    if not any(heading.upper() in _SUMMARY_HEADINGS for heading, _body in pieces):
        header, body = _header_and_body(document)
        lines = [line for line in header if line.strip()]
        lines.extend(["", "PROFESSIONAL SUMMARY", paragraph, ""])
        lines.extend(body)
        return "\n".join(lines).strip() + "\n"
    output: list[str] = []
    for heading, body in pieces:
        if heading.upper() in _SUMMARY_HEADINGS:
            output.extend([heading, paragraph, ""])
            continue
        if heading:
            output.append(heading)
        output.extend(body)
    return "\n".join(output).strip() + "\n"


def _summary_is_grounded(summary: str, document: str, job: dict[str, Any]) -> bool:
    text = re.sub(r"\s+", " ", summary or "").strip()
    if not (250 <= len(text) <= 1100):
        return False
    if _HEADING_LINE_RE.search(text) or text.startswith(("-", "•")):
        return False
    original = document.lower()
    title = str(job.get("title") or "").strip().lower()
    company = str(job.get("company") or "").strip().lower()
    title_words = set(re.findall(r"[a-z0-9]+", title))
    for skill in job.get("skills") or []:
        key = re.sub(r"\s+", " ", str(skill or "")).strip().lower()
        if len(key) < 3 or key in {title, company} or key in original:
            continue
        if title_words and all(word in title_words for word in re.findall(r"[a-z0-9]+", key)):
            continue
        if key in text.lower():
            return False
    return True


_COMMENTARY_RE = re.compile(
    r"(?i)^(here is|here's|sure[,.]|tailored resume|updated resume|rewritten resume|the (?:tailored|updated) resume)\b"
)
_EXPERIENCE_HEADINGS = {"PROFESSIONAL EXPERIENCE", "EXPERIENCE", "WORK EXPERIENCE", "WORK HISTORY"}


def _accept_ollama_resume(original: str, raw: str, job: dict[str, Any]) -> str:
    """Keep the real header and sections. Drop a rewrite that invents skills or loses the career history."""
    cleaned = _clean_model_text(raw)
    if len(cleaned) < 40:
        return ""
    header, _body = _header_and_body(original.strip())
    _new_header, new_body = _header_and_body(cleaned)
    if not new_body:
        return ""
    merged = "\n".join([line for line in header if line.strip()] + [""] + new_body).strip()
    restored = _restore_missing_sections(merged, original)
    if restored is None or not _rewrite_keeps_facts(original, restored, job):
        return ""
    return restored.strip() + "\n"


def _clean_model_text(raw: str) -> str:
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"(?is)<think>.*?</think>", "", text).strip()
    text = re.sub(r"^```[a-zA-Z0-9]*\n", "", text)
    text = re.sub(r"\n```$", "", text).strip()
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and (not lines[0].strip() or _COMMENTARY_RE.match(lines[0].strip())):
        lines.pop(0)
    return "\n".join(lines).strip()


def _header_and_body(text: str) -> tuple[list[str], list[str]]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if _HEADING_LINE_RE.match(line.strip()):
            return lines[:index], lines[index:]
    return lines, []


def _restore_missing_sections(merged: str, original: str) -> str | None:
    """Put back any section the model dropped. A missing work history means the rewrite is unusable."""
    present = {line.strip().upper() for line in merged.splitlines() if _HEADING_LINE_RE.match(line.strip())}
    extra: list[str] = []
    for heading, body in _split_resume_sections(original):
        if not heading or heading.upper() in present:
            continue
        if heading.upper() in _EXPERIENCE_HEADINGS:
            return None
        extra.extend(["", heading, *body])
    if not extra:
        return merged
    return merged.rstrip() + "\n" + "\n".join(extra).strip() + "\n"


def _dated_years(text: str) -> set[str]:
    """Years that sit on a role date line, not a year mentioned only in prose."""
    years: set[str] = set()
    for line in (text or "").splitlines():
        if not re.search(
            r"(?i)\b(?:19|20)\d{2}\s*[-–—/]\s*(?:present|current|now|(?:19|20)\d{2})\b",
            line,
        ):
            continue
        years.update(re.findall(r"\b(?:19|20)\d{2}\b", line))
    return years


def _rewrite_keeps_facts(original: str, rewritten: str, job: dict[str, Any]) -> bool:
    original_len = len(original.strip())
    rewritten_len = len(rewritten.strip())
    if rewritten_len < max(200, int(original_len * 0.45)):
        return False
    if rewritten_len > int(original_len * 2.4) + 400:
        return False
    dated_years = _dated_years(original)
    if dated_years and any(year not in rewritten for year in dated_years):
        return False
    original_l = original.lower()
    rewritten_l = rewritten.lower()
    title = str(job.get("title") or "").strip().lower()
    company = str(job.get("company") or "").strip().lower()
    for skill in job.get("skills") or []:
        key = re.sub(r"\s+", " ", str(skill or "")).strip().lower()
        if len(key) < 3 or key in {title, company} or key in original_l:
            continue
        if key in rewritten_l:
            return False
    return True


def tailor_document_for_job(document: str, job: dict[str, Any]) -> str:
    """Aim the existing resume at the job without adding skills or dropping sections."""
    text = (document or "").strip()
    if len(text) < 40 or not job:
        return document
    keywords = _job_keywords(job)
    if not keywords:
        return document if document.endswith("\n") else document + "\n"
    sections = _split_resume_sections(text)
    summary_keys = {"PROFESSIONAL SUMMARY", "SUMMARY", "CAREER OBJECTIVE", "OBJECTIVE"}
    has_summary = any(heading.upper() in summary_keys for heading, _body in sections)
    lines: list[str] = []
    for heading, body in sections:
        key = heading.upper()
        if key in summary_keys:
            body = _enhance_summary(body, job, text)
        elif key in {"KEY ACHIEVEMENTS", "PROFESSIONAL EXPERIENCE", "EXPERIENCE", "WORK EXPERIENCE", "WORK HISTORY"}:
            body = _promote_complete_bullets(body, keywords)
        elif key in {"CORE COMPETENCIES", "SKILLS", "TECHNICAL SKILLS", "CORE SKILLS"}:
            body = _promote_skill_items(body, keywords)
        if heading:
            lines.append(heading)
        lines.extend(body)
        if not heading and not has_summary and any(line.strip() for line in body):
            has_summary = True
    rendered = "\n".join(line for line in lines if line is not None).strip()
    return rendered + "\n"


def _split_resume_sections(text: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    heading = ""
    body: list[str] = []
    for line in text.splitlines():
        if _HEADING_LINE_RE.match(line.strip()):
            sections.append((heading, body))
            heading = line.strip()
            body = []
            continue
        body.append(line)
    sections.append((heading, body))
    return sections


def _job_keywords(job: dict[str, Any]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for skill in job.get("skills") or []:
        text = str(skill or "").strip()
        key = text.lower()
        if len(text) >= 2 and key not in seen and key not in _TAILOR_STOP:
            seen.add(key)
            found.append(text)
    blob = f"{job.get('title') or ''} {job.get('description') or ''}"
    for word in re.findall(r"[A-Za-z][A-Za-z0-9+#.]{2,}", blob):
        key = word.lower()
        if key in seen or key in _TAILOR_STOP:
            continue
        seen.add(key)
        found.append(word)
    return found[:40]


_ADDED_SUMMARY_RE = re.compile(
    r"(?i)^(aligned to|targeting the|this experience is aimed at|bringing this record to)\b"
)
_GENERIC_STRENGTH = {
    "product", "products", "digital", "platform", "platforms", "data", "build", "building",
    "senior", "engineer", "manager", "leader", "team", "teams", "experience", "role",
    "insurance", "market", "business", "solutions", "development",
}


def _enhance_summary(lines: list[str], job: dict[str, Any], document: str) -> list[str]:
    """Keep the original summary and close it with the job, using only real overlap."""
    kept = [line for line in lines if line.strip() and not _ADDED_SUMMARY_RE.match(line.strip())]
    if not kept:
        return [line for line in lines if line.strip()]
    title = str(job.get("title") or "").strip()
    if not title:
        return kept
    company = str(job.get("company") or "").strip()
    target = f"{title} role at {company}" if company else f"{title} role"
    blob = " ".join(kept)
    if title.lower() in blob.lower() and (not company or company.lower() in blob.lower()):
        return kept
    strengths = _strengths_for_job(document, job)
    if strengths:
        close = f"Bringing this record to the {target}, with direct experience in {strengths}."
    else:
        close = f"Bringing this record to the {target}."
    return [*kept, close]


def _strengths_for_job(document: str, job: dict[str, Any]) -> str:
    """Phrases the job asks for that are already written in the resume."""
    resume = (document or "").lower()
    if len(resume) < 40:
        return ""
    found: list[str] = []
    seen: set[str] = set()

    def add(phrase: str) -> None:
        text = re.sub(r"\s+", " ", phrase).strip(" .,-")
        key = text.lower()
        if len(text) < 3 or key in seen or key in _TAILOR_STOP or key in _GENERIC_STRENGTH:
            return
        if key not in resume:
            return
        if all(word in _GENERIC_STRENGTH or word in _TAILOR_STOP for word in key.split()):
            return
        seen.add(key)
        found.append(text)

    skills = [str(skill).strip() for skill in (job.get("skills") or []) if str(skill).strip()]
    for skill in sorted(skills, key=len, reverse=True):
        add(skill)
    job_blob = " ".join(
        str(part or "")
        for part in (job.get("title"), job.get("description"), *skills)
    )
    for phrase in re.findall(r"[A-Za-z][A-Za-z0-9+#.]{2,}(?:\s+[A-Za-z][A-Za-z0-9+#.]{2,}){1,3}", job_blob):
        add(phrase)
        if len(found) >= 3:
            break
    found = found[:3]
    if not found:
        return ""
    if len(found) == 1:
        return found[0]
    return ", ".join(found[:-1]) + " and " + found[-1]


def _promote_complete_bullets(lines: list[str], keywords: list[str]) -> list[str]:
    """Move finished bullets that match the job ahead of the others. Keep every line."""
    result: list[str] = []
    index = 0
    while index < len(lines):
        if not _is_complete_bullet(lines[index]):
            result.append(lines[index])
            index += 1
            continue
        end = index
        while end < len(lines) and _is_complete_bullet(lines[end]):
            end += 1
        block = lines[index:end]
        ranked = sorted(enumerate(block), key=lambda item: (-_keyword_hits(item[1], keywords), item[0]))
        result.extend(line for _, line in ranked)
        index = end
    return result


def _is_complete_bullet(line: str) -> bool:
    return bool(re.match(r"^\s*[-•]\s+\S.{24,}", line or ""))


def _promote_skill_items(lines: list[str], keywords: list[str]) -> list[str]:
    """Move matching skill lines up only when each line is one skill, not a wrapped paragraph."""
    items = [line for line in lines if line.strip()]
    if len(items) < 2:
        return lines
    if any(len(line) > 80 or line.rstrip().endswith((",", ".", ":")) for line in items):
        return lines
    ranked = sorted(enumerate(items), key=lambda item: (-_keyword_hits(item[1], keywords), item[0]))
    return [line for _, line in ranked]


def _keyword_hits(line: str, keywords: list[str]) -> int:
    lowered = (line or "").lower()
    return sum(1 for keyword in keywords if keyword.lower() in lowered)


def _has_named_sections(text: str) -> bool:
    lowered = f"\n{(text or '').lower()}\n"
    needed = (
        "professional experience" in lowered or "work experience" in lowered or "\nexperience\n" in lowered,
        "\neducation\n" in lowered,
        "certification" in lowered or "competenc" in lowered or "\nskills\n" in lowered,
    )
    return sum(1 for item in needed if item) >= 2


def _normalize_section_titles(text: str) -> str:
    replacements = {
        r"(?im)^SUMMARY$": "PROFESSIONAL SUMMARY",
        r"(?im)^EXPERIENCE$": "PROFESSIONAL EXPERIENCE",
        r"(?im)^SKILLS$": "CORE COMPETENCIES",
    }
    out = text
    for pattern, title in replacements.items():
        out = re.sub(pattern, title, out)
    return out


def _restore_resume(text: str) -> str:
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    cleaned = cleaned.replace("\u2022", "- ").replace("•", "- ")
    cleaned = _separate_headings(cleaned)
    cleaned = _JOB_AD_RE.sub("", cleaned, count=1)
    lines = []
    started = False
    for raw in cleaned.splitlines():
        line = re.sub(r"[ \t]{2,}", " ", raw).strip()
        line = _PLAN_CREDITS_RE.sub("", line).strip(" .")
        if not line or _is_junk_line(line) or _is_job_ad_line(line) or line.lower() in {"benefits", "bonus"}:
            continue
        if not started and _is_posting_title(line):
            continue
        started = True
        lines.append(line)
    lines = _drop_posting_runs(lines)
    lines = _drop_repeated_lines(lines)
    lines = _drop_repeated_passages(lines)
    return "\n".join(lines).strip() + ("\n" if lines else "")


def _separate_headings(text: str) -> str:
    """Put every resume section title on its own line, even when two titles are glued together."""

    def place(match: re.Match[str]) -> str:
        raw = match.group(1)
        letters = [char for char in raw if char.isalpha()]
        upper = sum(1 for char in letters if char.isupper())
        mostly_caps = bool(letters) and upper / len(letters) >= 0.6
        at_line_start = match.start() == 0 or match.string[match.start() - 1] == "\n"
        rest_of_line = match.string[match.end() :].split("\n", 1)[0].strip()
        # A title in a sentence stays put. A real section title is its own line, or glued in capitals.
        if not mostly_caps and not (at_line_start and rest_of_line == ""):
            return raw
        return "\n" + raw.upper() + "\n"

    return _HEADING_RE.sub(place, text)


def _heading_count(text: str) -> int:
    return sum(1 for line in (text or "").splitlines() if _HEADING_LINE_RE.match(line.strip()))


def _is_junk_line(line: str) -> bool:
    lowered = line.lower().strip()
    if lowered in {"intermediate", "beginner", "advanced", "expert", "novice"}:
        return True
    if _UUID_RE.fullmatch(lowered):
        return True
    ids = _UUID_RE.findall(lowered)
    if len(ids) >= 2:
        return True
    return False


def _is_job_ad_line(line: str) -> bool:
    lowered = line.lower()
    if "$" in line and re.search(r"\d", line):
        return True
    if "discretionary bonus" in lowered or "mid-senior" in lowered:
        return True
    if _PLAN_CREDITS_RE.search(lowered):
        return True
    return False


def _is_posting_title(line: str) -> bool:
    """A bare job-board title, not a resume sentence, date, or bullet."""
    if _HEADING_LINE_RE.match(line) or _DATE_RE.search(line):
        return False
    if line.startswith("-") or len(line) > 140:
        return False
    words = line.split()
    if not (2 <= len(words) <= 16):
        return False
    return _ROLE_RE.search(line) is not None


def _drop_posting_runs(lines: list[str]) -> list[str]:
    """Drop a streak of saved job titles. One title next to a date or bullet stays."""
    kept: list[str] = []
    index = 0
    while index < len(lines):
        if not _is_posting_title(lines[index]):
            kept.append(lines[index])
            index += 1
            continue
        end = index
        while end < len(lines) and _is_posting_title(lines[end]):
            end += 1
        if end - index >= 3:
            index = end
            continue
        kept.extend(lines[index:end])
        index = end
    return kept


def _drop_repeated_lines(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for line in lines:
        key = re.sub(r"\s+", " ", line).strip().lower()
        if len(key) >= 40 and key in seen:
            continue
        if len(key) >= 40:
            seen.add(key)
        kept.append(line)
    return kept


def _drop_repeated_passages(lines: list[str]) -> list[str]:
    """Remove a second copy of the same paragraph when line breaks differ."""
    words: list[str] = []
    owners: list[int] = []
    for index, line in enumerate(lines):
        for word in line.split():
            words.append(word)
            owners.append(index)
    lower = [word.lower() for word in words]
    window = 10
    seen: dict[tuple[str, ...], int] = {}
    drop_lines: set[int] = set()
    for index in range(0, max(len(lower) - window + 1, 0)):
        key = tuple(lower[index : index + window])
        if key not in seen:
            seen[key] = index
            continue
        start = seen[key]
        size = 0
        while start + size < index and index + size < len(lower) and lower[start + size] == lower[index + size]:
            size += 1
        if size >= window:
            drop_lines.update(owners[index : index + size])
        break
    if not drop_lines:
        return lines
    return [line for index, line in enumerate(lines) if index not in drop_lines]


def _strip_tailor_suffix(text: str) -> str:
    cleaned = re.sub(r"\s*Core strengths for this role include [^.]*\.", "", text)
    cleaned = re.sub(r"\s*Prepared for [^.]*\.", "", cleaned)
    return cleaned.strip() + ("\n" if cleaned.strip() else "")


def _profile_blob(profile: dict[str, Any]) -> str:
    return "\n".join(
        part
        for part in (
            str(profile.get("resume_document") or "").strip(),
            str(profile.get("resume_text") or "").strip(),
            str(profile.get("professional_summary") or "").strip(),
            str(profile.get("headline") or "").strip(),
            "\n".join(str(item) for item in (profile.get("achievements") or []) if item),
        )
        if part
    )


def _original_sections(text: str) -> dict[str, str]:
    """Section bodies from the saved resume, including achievements and competencies."""
    cleaned = _separate_headings(text or "")
    found: dict[str, str] = {}
    aliases = {
        "PROFESSIONAL SUMMARY": "summary",
        "SUMMARY": "summary",
        "CAREER OBJECTIVE": "summary",
        "OBJECTIVE": "summary",
        "KEY ACHIEVEMENTS": "achievements",
        "ACHIEVEMENTS": "achievements",
        "CORE COMPETENCIES": "competencies",
        "COMPETENCIES": "competencies",
        "TECHNICAL SKILLS": "competencies",
        "CORE SKILLS": "competencies",
        "SKILLS": "competencies",
        "CERTIFICATIONS": "certifications",
        "EDUCATION": "education",
    }
    heading = ""
    body: list[str] = []

    def save() -> None:
        key = aliases.get(heading.upper())
        block = "\n".join(line for line in body if line.strip()).strip()
        if key and block and key not in found:
            found[key] = block

    for line in cleaned.splitlines():
        if _HEADING_LINE_RE.match(line.strip()):
            save()
            heading = line.strip()
            body = []
            continue
        body.append(line)
    save()
    return found


def _usable_person_name(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" .")
    if not text or _PLAN_CREDITS_RE.search(text):
        return ""
    words = text.split()
    if not (2 <= len(words) <= 4):
        return ""
    if any(word.lower().strip(".") in _NAME_BLOCK for word in words):
        return ""
    if not all(re.fullmatch(r"[A-Za-z][A-Za-z.'\-]*", word) for word in words):
        return ""
    if not all(word[0].isupper() for word in words):
        return ""
    return text


def _name_from_text(text: str) -> str:
    for line in (text or "").splitlines()[:12]:
        found = _usable_person_name(line)
        if found:
            return found
    return ""


def _usable_location(location: str, text: str) -> bool:
    loc = str(location or "").strip()
    if not loc or _PLAN_CREDITS_RE.search(loc):
        return False
    lowered = loc.lower()
    if "marketplace" in lowered or lowered.startswith("insurance "):
        return False
    for line in (text or "").splitlines():
        if loc in line and line.strip() != loc and len(line) > len(loc) + 8:
            return False
    return True


def _usable_headline(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or _PLAN_CREDITS_RE.search(text) or _is_posting_title(text):
        return ""
    if "|" in text and len(text) <= 120 and "@" not in text:
        return text
    return ""


def _job_blurbs(experience: list[dict]) -> str:
    parts = []
    for item in experience or []:
        parts.extend(str(bullet) for bullet in (item.get("bullets") or []))
        if item.get("company"):
            parts.append(str(item.get("company")))
    return "\n".join(parts).lower()


def _is_job_blurb(text: str, experience: list[dict]) -> bool:
    sample = re.sub(r"\s+", " ", text or "").strip().lower()
    if len(sample) < 40:
        return False
    blurbs = _job_blurbs(experience)
    return len(sample) >= 40 and sample[:80] in blurbs


def _pick_summary(parsed: str, stored: str, blob: str, experience: list[dict]) -> str:
    for candidate in (stored, _original_sections(blob).get("summary", ""), parsed):
        text = re.sub(r"\s+", " ", str(candidate or "")).strip()
        if text and not _is_job_blurb(text, experience) and not _PLAN_CREDITS_RE.search(text):
            return text
    return ""


def _company_from_blurb(text: str) -> str:
    first = str(text or "").strip().split("\n", 1)[0].strip()
    match = _COMPANY_LEAD_RE.match(first)
    return match.group(1) if match else ""


def _clean_certifications(items: list[str]) -> list[str]:
    kept = []
    for item in items:
        text = re.sub(r"^[\-•]\s*", "", str(item or "")).strip(" .")
        if not text or _ISSUER_ONLY_RE.match(text) or _PLAN_CREDITS_RE.search(text):
            continue
        kept.append(text)
    return _unique(kept)


def _polish_document(document: str, profile: dict[str, Any], sections: dict[str, str]) -> str:
    """Put the real name and any original sections back onto a rebuilt resume."""
    text = _PLAN_CREDITS_RE.sub("", document or "")
    lines = [re.sub(r"[ \t]{2,}", " ", line).rstrip() for line in text.splitlines()]
    lines = [line for line in lines if line.strip()]
    if not lines:
        return document
    name = (
        _name_from_text(_profile_blob(profile))
        or _usable_person_name(_join_name(profile.get("first_name"), profile.get("last_name")) or "")
        or _usable_person_name(lines[0])
    )
    if name and not _usable_person_name(lines[0]):
        lines[0] = name
    headline = _usable_headline(str(profile.get("headline") or ""))
    if headline and headline.lower() not in "\n".join(lines).lower():
        lines.insert(1, headline)
    source_header = _source_header(_profile_blob(profile), name or lines[0])
    if len(source_header) >= 2:
        body = lines
        while body and not _HEADING_LINE_RE.match(body[0]):
            body = body[1:]
        lines = source_header + body
    city = str(profile.get("city") or "").strip()
    if city:
        lines = [_replace_fake_location(line, city) for line in lines]
    rendered = _replace_weak_sections("\n".join(lines), sections, profile)
    return rendered.strip() + "\n"


def _source_header(blob: str, name: str) -> list[str]:
    """The original name, headline, and contact lines when the saved resume has them."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in (blob or "").splitlines() if line.strip()]
    if not lines:
        return []
    start = 0
    wanted = (name or "").strip().lower()
    if wanted:
        for index, line in enumerate(lines[:12]):
            if wanted in line.lower() and len(line) <= 60:
                start = index
                break
    header = [lines[start]]
    for line in lines[start + 1 : start + 4]:
        if _HEADING_LINE_RE.match(line) or _PLAN_CREDITS_RE.search(line):
            break
        if _usable_headline(line) or _source_contact_line(line):
            header.append(line)
            continue
        break
    return header if len(header) >= 2 else []


def _source_contact_line(line: str) -> bool:
    text = (line or "").strip()
    if "@" in text or re.search(r"(?i)\biqama\b", text):
        return True
    digits = re.sub(r"\D", "", text)
    return len(digits) >= 8 and any(mark in text for mark in ("|", "•", "·", ","))


def _replace_fake_location(line: str, city: str) -> str:
    if "|" not in line:
        return line
    parts = [part.strip() for part in line.split("|")]
    changed = False
    for index, part in enumerate(parts):
        lowered = part.lower()
        if "marketplace" in lowered or lowered.startswith("insurance ") or _PLAN_CREDITS_RE.search(part):
            parts[index] = city
            changed = True
    if not changed:
        return line
    return " | ".join(_unique(parts))


def _replace_weak_sections(text: str, sections: dict[str, str], profile: dict[str, Any]) -> str:
    pieces = _split_resume_sections(text)
    summary = sections.get("summary") or ""
    achievements = sections.get("achievements") or ""
    if not achievements:
        extra = [str(item).strip() for item in (profile.get("achievements") or []) if str(item).strip()]
        if extra:
            achievements = "\n".join(f"- {item}" for item in extra)
    competencies = sections.get("competencies") or ""
    certifications = sections.get("certifications") or ""
    output: list[str] = []
    saw_summary = False
    inserted_achievements = "KEY ACHIEVEMENTS" in text.upper()
    for heading, body in pieces:
        key = heading.upper()
        if key in {"PROFESSIONAL SUMMARY", "SUMMARY", "CAREER OBJECTIVE", "OBJECTIVE"}:
            saw_summary = True
            current = " ".join(line.strip() for line in body if line.strip())
            if summary and (not current or _summary_is_weaker(current, summary)):
                body = [summary]
        elif key in {"CORE COMPETENCIES", "SKILLS", "TECHNICAL SKILLS", "CORE SKILLS"} and competencies:
            if len(competencies) > len(" ".join(body)) + 20:
                body = competencies.splitlines()
        elif key in {"CERTIFICATIONS"} and certifications:
            if certifications.count("\n") > len([line for line in body if line.strip()]):
                body = certifications.splitlines()
            else:
                body = [line for line in body if not _ISSUER_ONLY_RE.match(re.sub(r"^[\-•]\s*", "", line).strip(" ."))]
        if heading:
            output.append(heading)
        output.extend(body)
        if saw_summary and achievements and not inserted_achievements:
            output.extend(["", "KEY ACHIEVEMENTS", *achievements.splitlines()])
            inserted_achievements = True
    if achievements and not inserted_achievements:
        output.extend(["", "KEY ACHIEVEMENTS", *achievements.splitlines()])
    if competencies and "CORE COMPETENCIES" not in text.upper() and "\nSKILLS\n" not in f"\n{text.upper()}\n":
        output.extend(["", "CORE COMPETENCIES", *competencies.splitlines()])
    return "\n".join(output)


def _summary_is_weaker(current: str, original: str) -> bool:
    current_l = current.lower()
    original_l = original.lower()
    if original_l[:40] in current_l:
        return False
    if "is the leading" in current_l or "is a " in current_l[:80]:
        return True
    return "years of experience" in original_l and "years of experience" not in current_l


def resume_from_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Shape a stored user profile into the resume JSON the tailor expects."""
    source_text = str(profile.get("resume_document") or profile.get("resume_text") or "").strip()
    parsed = _parsed_resume_text(source_text)
    parsed_profile = (parsed or {}).get("profile") or {}
    parsed_name = _usable_person_name(str(parsed_profile.get("full_name") or ""))
    name = (
        _name_from_text(source_text)
        or parsed_name
        or _usable_person_name(_join_name(profile.get("first_name"), profile.get("last_name")) or "")
    )
    location = _join_place(profile.get("city"), profile.get("state"), profile.get("country"))
    parsed_location = str(parsed_profile.get("location") or "")
    if parsed_location and _usable_location(parsed_location, source_text):
        location = parsed_location
    history = _experience_from_history(profile.get("work_history") or [])
    history = [item for item in history if _looks_like_real_job(item)]
    parsed_jobs = list((parsed or {}).get("experience") or [])
    experience = _richer_experience(history, parsed_jobs)
    schools = _education_from_profile(profile.get("education") or [])
    if not schools and parsed:
        schools = list(parsed.get("education") or [])
    summary = _pick_summary(
        str(parsed_profile.get("summary") or ""),
        str(profile.get("professional_summary") or ""),
        source_text,
        experience,
    ) or None
    return {
        "profile": {
            "full_name": name,
            "email": parsed_profile.get("email") or _or_none(profile.get("email")),
            "phone": parsed_profile.get("phone") or _or_none(profile.get("phone")),
            "phones": [],
            "location": location,
            "linkedin": parsed_profile.get("linkedin") or _or_none(profile.get("linkedin_url")),
            "github": parsed_profile.get("github") or _or_none(profile.get("github_url")),
            "website": parsed_profile.get("website") or _or_none(profile.get("portfolio_url") or profile.get("personal_website")),
            "summary": summary,
        },
        "skills": _unique([*((parsed or {}).get("skills") or []), *_real_skills(profile.get("skills") or [])]),
        "experience": experience,
        "education": schools,
        "projects": list((parsed or {}).get("projects") or []),
        "certifications": _clean_certifications(
            [*((parsed or {}).get("certifications") or []), *_real_skills(profile.get("certifications") or [])]
        ),
        "languages": list((parsed or {}).get("languages") or []),
    }


def _parsed_resume_text(text: str) -> dict[str, Any] | None:
    cleaned = str(text or "").strip()
    if len(cleaned) < 40:
        return None
    try:
        return parse_resume_text(cleaned)
    except ResumeParseError:
        return None


def _looks_like_real_job(item: dict) -> bool:
    """Keep real work history. Drop bare job-board titles with no company, dates, or bullets."""
    if item.get("company") or item.get("start_date") or item.get("bullets"):
        return True
    title = str(item.get("title") or "").strip()
    return bool(title) and not _is_posting_title(title)


def _experience_from_history(rows: list[dict]) -> list[dict]:
    found = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("job_title") or row.get("title") or "").strip()
        company = str(row.get("company") or "").strip()
        end = str(row.get("end_date") or "").strip()
        description = str(row.get("description") or row.get("responsibilities") or "").strip()
        if not company:
            company = _company_from_blurb(description)
        if not title and not company:
            continue
        bullets = [line.lstrip("-• ").strip() for line in description.splitlines() if line.strip()]
        found.append(
            {
                "title": title or None,
                "company": company or None,
                "location": None,
                "start_date": str(row.get("start_date") or "").strip() or None,
                "end_date": end or None,
                "is_current": end.lower() in {"", "present", "current"},
                "bullets": bullets[:30],
            }
        )
    return found


def _education_from_profile(rows: list[dict]) -> list[dict]:
    found = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        institution = str(row.get("school") or row.get("institution") or "").strip()
        degree = str(row.get("degree") or "").strip()
        field = str(row.get("field_of_study") or row.get("field") or "").strip()
        if not institution and not degree and not field:
            continue
        found.append(
            {
                "degree": degree or None,
                "field": field or None,
                "institution": institution or None,
                "start_year": None,
                "end_year": str(row.get("graduation_year") or row.get("end_year") or "").strip() or None,
                "gpa": str(row.get("gpa") or row.get("grade") or "").strip() or None,
            }
        )
    return found


def _richer_experience(history: list[dict], parsed_jobs: list[dict]) -> list[dict]:
    if _bullet_weight(parsed_jobs) > _bullet_weight(history):
        return parsed_jobs
    return history or parsed_jobs


def _bullet_weight(items: list[dict]) -> int:
    return sum(len(str(bullet)) for item in items for bullet in (item.get("bullets") or []))


def _has_content(resume: dict) -> bool:
    profile = resume.get("profile") or {}
    summary = str(profile.get("summary") or "").strip()
    return bool(
        resume.get("skills")
        or resume.get("experience")
        or resume.get("education")
        or resume.get("certifications")
        or resume.get("projects")
        or len(summary) >= 40
    )


def _join_name(*parts) -> str | None:
    text = " ".join(str(part).strip() for part in parts if str(part or "").strip())
    return text or None


def _join_place(*parts) -> str | None:
    text = ", ".join(str(part).strip() for part in parts if str(part or "").strip())
    return text or None


def _or_none(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def _real_skills(values) -> list[str]:
    kept = []
    for value in values:
        text = str(value or "").strip()
        if not text or _is_junk_line(text):
            continue
        kept.append(text)
    return kept


def _unique(values) -> list[str]:
    found = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        found.append(text)
    return found
