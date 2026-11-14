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

_SYSTEM = """You rewrite a resume so it reads as a fit for one specific job.
Use only facts already written in the resume.

Rules:
- Do not add employers, job titles, dates, degrees, certifications, tools, or achievements that are not in the resume.
- Do not add a skill from the job unless that exact skill already appears in the resume.
- Keep the same section headings. Put each heading on its own line in CAPITAL LETTERS.
- Keep every role, date, school, and certification.
- Rewrite the professional summary so it targets the job title, using only overlap already in the resume.
- Rewrite experience bullets so the wording matches the job description where the resume already shows that work. Start each bullet with "- ".
- Keep the opening name and contact lines unchanged.
- Reply with the resume only. No markdown, no commentary, no code fences.
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


def rewrite_resume(resume: str, job: dict) -> str:
    """Ask Ollama for a full resume aimed at this job title and description."""
    if not ollama_enabled():
        raise OllamaError("Ollama resume enhance is turned off")
    text = (resume or "").strip()
    if len(text) < 40:
        raise OllamaError("Resume is too short to enhance")
    if len(text) > 12000:
        raise OllamaError("Resume is too long for one Ollama pass")
    title = str(job.get("title") or "").strip()
    description = _clip_description(str(job.get("description") or ""))
    if not title and not description:
        raise OllamaError("Job has no title or description")
    model = _model_name()
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _user_prompt(text, job, title, description)},
        ],
        "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": 4096},
    }
    parsed = _request("/api/chat", payload, _timeout())
    message = parsed.get("message") or {}
    content = _strip_think(str(message.get("content") or ""))
    if len(content) < 40:
        raise OllamaError("Ollama returned an empty resume")
    log.info("Ollama enhanced resume with model %s for %s", model, title or "untitled job")
    return content


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


def _user_prompt(resume: str, job: dict, title: str, description: str) -> str:
    company = str(job.get("company") or "").strip() or "(not listed)"
    skills = _skill_names(job.get("skills"))
    skill_block = "\n".join(f"- {skill}" for skill in skills[:25]) or "- (none listed)"
    posting = description or "(no description stored)"
    return (
        f"JOB TITLE:\n{title or '(not listed)'}\n\n"
        f"COMPANY:\n{company}\n\n"
        f"JOB SKILLS TO EMPHASIZE ONLY IF ALREADY IN THE RESUME:\n{skill_block}\n\n"
        f"JOB DESCRIPTION:\n{posting}\n\n"
        f"RESUME:\n{resume}"
    )


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
        value = float(_env("OLLAMA_TIMEOUT", "90") or "90")
    except ValueError:
        value = 90
    return max(10.0, min(value, 180.0))


def _clip_description(text: str, limit: int = 4000) -> str:
    plain = _plain_text(text)
    if len(plain) <= limit:
        return plain
    head = plain[:2600].rsplit(" ", 1)[0]
    tail = plain[-1200:].split(" ", 1)[-1]
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


def _skill_names(value) -> list[str]:
    if isinstance(value, str):
        items = re.split(r"[,;\n|]", value)
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = []
    names = []
    seen = set()
    for item in items:
        if isinstance(item, dict):
            text = str(item.get("name") or item.get("skill") or "")
        else:
            text = str(item or "")
        text = re.sub(r"\s+", " ", text).strip()
        key = text.lower()
        if len(text) < 2 or key in seen:
            continue
        seen.add(key)
        names.append(text)
    return names


def _strip_think(text: str) -> str:
    cleaned = re.sub(r"(?is)<think>.*?</think>", "", text or "")
    if re.search(r"(?i)</think>", cleaned):
        cleaned = re.split(r"(?i)</think>", cleaned)[-1]
    return cleaned.strip()


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()
