"""Rewrite a saved resume toward one job with the Ollama instance on this server."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from html import unescape
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger("job-discovery")

_PREFERRED = (
    "qwen2.5",
    "qwen3",
    "llama3.3",
    "llama3.1",
    "llama3",
    "llama3.2",
    "mistral",
    "gemma3",
    "gemma2",
    "phi4",
    "phi3",
)
_SKIP_MODEL = ("embed", "vision", "llava", "bert")
_STATUS_CACHE: dict = {"at": 0.0, "value": None}
_MODEL_CACHE: dict = {"at": 0.0, "name": ""}

_SUMMARY_SYSTEM = """You write a professional summary for an existing resume aimed at one job.
Use only facts already written in the resume.

Rules:
- Return only the summary paragraph. No heading, no bullets, no markdown, no labels.
- 4 to 6 sentences. Third person, not "I".
- Open with the person's seniority, years, and domain from the resume.
- Name the target job title once.
- Keep real employers, products, and numbers already in the resume.
- Do not invent tools, certifications, degrees, or employers.
- Do not say the person already works at the hiring company.
"""

_BULLET_SYSTEM = """You rewrite resume experience bullets for one job.
Use only facts already written in each bullet.

Rules:
- Return the same number of bullets, in the same order.
- One bullet per line. Each line starts with "- ".
- Keep the same facts, numbers, employers, and products.
- Only change wording so the bullet fits the target job.
- Do not add or remove bullets. No headings. No commentary.
"""


class OllamaError(Exception):
    """The local Ollama call could not produce a resume."""


def ollama_enabled() -> bool:
    return _env("OLLAMA_ENABLED", "true").lower() not in {"0", "false", "no", "off"}


def ollama_host() -> str:
    return _env("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")


def ollama_status() -> dict:
    """Short reachability check for /api/health. Cached so a down daemon does not stall every probe."""
    now = time.time()
    cached = _STATUS_CACHE.get("value")
    if cached is not None and now - float(_STATUS_CACHE.get("at") or 0) < 20:
        return cached
    status = _status_now()
    _STATUS_CACHE["at"] = now
    _STATUS_CACHE["value"] = status
    return status


def active_model() -> str:
    """Model name Ollama will use, or an empty string when none can be chosen."""
    try:
        return _model_name()
    except OllamaError:
        return ""


def complete(
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    num_predict: int = 900,
    timeout: float | None = None,
) -> str:
    """One non-streaming Ollama chat completion."""
    if not ollama_enabled():
        raise OllamaError("Ollama resume generation is turned off")
    limit = timeout if timeout is not None else min(_timeout(), 120)
    return _chat(system, user, temperature, num_predict, limit)


def rewrite_for_job(resume: str, job: dict) -> tuple[str, list[str]]:
    """Ask Ollama for a short job summary. Bullet rewrite is optional and must not block the summary."""
    if not ollama_enabled():
        raise OllamaError("Ollama resume enhance is turned off")
    text = (resume or "").strip()
    if len(text) < 40:
        raise OllamaError("Resume is too short to enhance")
    title = str(job.get("title") or "").strip()
    description = _clip_description(str(job.get("description") or ""), limit=1800)
    if not title and not description:
        raise OllamaError("Job has no title or description")

    raw = _chat(
        _SUMMARY_SYSTEM,
        _summary_prompt(text[:1600], job, title, description),
        temperature=0.2,
        num_predict=220,
        timeout=min(_timeout(), 90),
    )
    summary = _one_paragraph(raw)
    if len(summary) < 60:
        raise OllamaError(f"Ollama summary was too short: {summary[:80]!r}")

    bullets: list[str] = []
    bullet_src = _experience_bullets(text)
    if bullet_src:
        try:
            raw_bullets = _chat(
                _BULLET_SYSTEM,
                _bullet_prompt(bullet_src[:12], job, title, description),
                temperature=0.2,
                num_predict=900,
                timeout=min(_timeout(), 80),
            )
            bullets = _parse_bullets(raw_bullets)
        except OllamaError as exc:
            log.warning("Ollama bullet rewrite skipped: %s", exc)

    log.info("Ollama wrote a summary with model %s for %s", _model_name(), title or "untitled job")
    return summary, bullets


def _status_now() -> dict:
    host = ollama_host()
    base = {"enabled": ollama_enabled(), "host": host}
    if not ollama_enabled():
        return {**base, "ok": False, "error": "disabled"}
    try:
        names = _model_names(timeout=2.5)
    except OllamaError as exc:
        return {**base, "ok": False, "error": str(exc)[:200]}
    if not names:
        return {**base, "ok": False, "error": "Ollama has no models installed"}
    return {**base, "ok": True, "model": _choose_model(names), "models": names[:8]}


def _summary_prompt(resume: str, job: dict, title: str, description: str) -> str:
    company = str(job.get("company") or "").strip() or "(not listed)"
    posting = description or "(no description stored)"
    return (
        f"TARGET JOB TITLE:\n{title or '(not listed)'}\n\n"
        f"COMPANY:\n{company}\n\n"
        f"JOB DESCRIPTION:\n{posting}\n\n"
        f"RESUME:\n{resume}"
    )


def _bullet_prompt(bullets: list[str], job: dict, title: str, description: str) -> str:
    company = str(job.get("company") or "").strip() or "(not listed)"
    body = "\n".join(f"- {item}" for item in bullets)
    return (
        f"TARGET JOB TITLE:\n{title or '(not listed)'}\n\n"
        f"COMPANY:\n{company}\n\n"
        f"JOB DESCRIPTION:\n{description or '(no description stored)'}\n\n"
        f"BULLETS TO REWRITE ({len(bullets)} lines):\n{body}"
    )


def _experience_bullets(resume: str) -> list[str]:
    found: list[str] = []
    in_experience = False
    for line in (resume or "").splitlines():
        stripped = line.strip()
        if re.fullmatch(
            r"(?i)professional experience|experience|work experience|work history|education|"
            r"core competencies|skills|certifications|key achievements|projects|languages",
            stripped,
        ):
            in_experience = stripped.lower() in {
                "professional experience",
                "experience",
                "work experience",
                "work history",
            }
            continue
        if not in_experience:
            continue
        if re.match(r"^[\-•]\s+\S", stripped):
            item = re.sub(r"^[\-•]\s*", "", stripped).strip()
            if item and not re.match(r"^[A-Z][\w&.'-]{1,40}(?:\s+[A-Z][\w&.'-]+){0,5}\s+is\b", item):
                found.append(item)
    return found


def _chat(system: str, user: str, temperature: float, num_predict: int, timeout: float = 70) -> str:
    model = _model_name()
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "keep_alive": "30m",
        "options": {
            "temperature": temperature,
            "num_ctx": 2048,
            "num_predict": num_predict,
        },
    }
    parsed = _request("/api/chat", payload, timeout)
    message = parsed.get("message") or {}
    content = _strip_think(str(message.get("content") or ""))
    if len(content.strip()) < 20:
        raise OllamaError("Ollama returned an empty response")
    return content


def _model_name() -> str:
    chosen = _env("OLLAMA_MODEL")
    if chosen:
        return chosen
    now = time.time()
    cached = str(_MODEL_CACHE.get("name") or "")
    if cached and now - float(_MODEL_CACHE.get("at") or 0) < 600:
        return cached
    names = _model_names(timeout=5)
    if not names:
        raise OllamaError("Ollama has no models installed")
    name = _choose_model(names)
    _MODEL_CACHE["at"] = now
    _MODEL_CACHE["name"] = name
    return name


def _choose_model(names: list[str]) -> str:
    ranked = sorted(names, key=_model_rank)
    return ranked[0]


def _model_rank(name: str) -> tuple:
    base = name.split(":", 1)[0].lower()
    if any(token in base for token in _SKIP_MODEL):
        return (9, 0, name)
    for index, prefix in enumerate(_PREFERRED):
        if base == prefix or base.startswith(prefix + "-") or base.startswith(prefix + "."):
            return (0, index, name)
    return (1, 0, name)


def _model_names(timeout: float) -> list[str]:
    payload = _request("/api/tags", None, timeout)
    names = []
    for item in payload.get("models") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("model") or "").strip()
        if name:
            names.append(name)
    return names


def _request(path: str, payload: dict | None, timeout: float) -> dict:
    url = ollama_host() + path
    data = None
    headers = {"Accept": "application/json"}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise OllamaError(f"Ollama HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise OllamaError(f"Ollama is not reachable at {ollama_host()}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise OllamaError("Ollama timed out") from exc
    try:
        parsed = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise OllamaError("Ollama returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise OllamaError("Ollama returned an unexpected payload")
    return parsed


def _timeout() -> float:
    try:
        value = float(_env("OLLAMA_TIMEOUT", "120") or "120")
    except ValueError:
        value = 120
    return max(20.0, min(value, 240.0))


def _clip_description(text: str, limit: int = 3500) -> str:
    plain = _plain_text(text)
    if len(plain) <= limit:
        return plain
    head = plain[:2300].rsplit(" ", 1)[0]
    tail = plain[-1000:].split(" ", 1)[-1]
    return f"{head}\n...\n{tail}"


def _plain_text(text: str) -> str:
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", text or "")
    cleaned = re.sub(r"(?i)</p>", "\n", cleaned)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = unescape(cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _parse_bullets(text: str) -> list[str]:
    bullets: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not re.match(r"^[\-\u2022•]\s+\S", stripped):
            continue
        item = re.sub(r"^[\-\u2022•]\s*", "", stripped).strip()
        if len(item) < 20:
            continue
        bullets.append(item)
    return bullets


def _one_paragraph(text: str) -> str:
    cleaned = _strip_think(text)
    cleaned = re.sub(r"^```[a-zA-Z0-9]*\n|\n```$", "", cleaned).strip()
    cleaned = re.sub(
        r"(?i)^(professional summary|summary|here is|here's|bullets?)\s*:?\s*",
        "",
        cleaned,
    ).strip()
    lines = [re.sub(r"^[\-•]\s*", "", line).strip() for line in cleaned.splitlines() if line.strip()]
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def _strip_think(text: str) -> str:
    cleaned = re.sub(r"(?is)<think>.*?</think>", "", text or "")
    if re.search(r"(?i)</think>", cleaned):
        cleaned = re.split(r"(?i)</think>", cleaned)[-1]
    return cleaned.strip()


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()
