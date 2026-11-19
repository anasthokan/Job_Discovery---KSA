"""WhatsApp reminder one day before a tech event.

A user is notified when their profile city matches the event city and their
skills match the event domain. Each phone receives each event once.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jobscraper.events import domain_matches, load_events, location_matches

log = logging.getLogger("jobscraper.event_alerts")

_ROOT = Path(__file__).resolve().parent.parent
SENT_FILE = _ROOT / "data" / "event_alerts.json"
ALERTS_TABLE = "event_whatsapp_alerts"
_TZ = ZoneInfo("Asia/Riyadh")

CREATE_EVENT_ALERTS_SQL = f"""
CREATE TABLE IF NOT EXISTS {ALERTS_TABLE} (
  id BIGINT NOT NULL AUTO_INCREMENT,
  phone VARCHAR(64) NOT NULL,
  event_id VARCHAR(160) NOT NULL,
  user_id VARCHAR(64) NULL,
  title VARCHAR(512) NULL,
  city VARCHAR(120) NULL,
  start_date DATE NULL,
  message TEXT NULL,
  sent_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_event_whatsapp_alert (phone, event_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

# Profile words that count as an event domain. Short tokens are matched whole.
_DOMAIN_PHRASES = (
    ("ai", ("artificial intelligence", "machine learning", "deep learning", "generative ai", "genai", "llm")),
    ("data", ("data science", "data analyst", "data engineer", "data analytics", "business intelligence")),
    ("fintech", ("fintech", "financial technology")),
    ("cybersecurity", ("cybersecurity", "cyber security", "information security", "infosec")),
    ("proptech", ("proptech", "prop tech", "real estate technology")),
    ("healthtech", ("healthtech", "health tech", "digital health")),
    ("cloud", ("cloud computing", "saas", "devsecops")),
    ("web3", ("web3", "blockchain")),
    ("gaming", ("game development", "gaming")),
    ("govtech", ("govtech", "gov tech")),
    ("enterprise", ("enterprise tech", "enterprise software")),
    ("deep tech", ("deep tech", "deeptech")),
    ("logistics", ("logistics", "supply chain")),
    ("startup", ("startups", "entrepreneurship")),
)
_SHORT_SKILLS = {
    "ai": "ai",
    "ml": "ai",
    "genai": "ai",
    "llm": "ai",
    "data": "data",
    "fintech": "fintech",
    "cybersecurity": "cybersecurity",
    "security": "cybersecurity",
    "proptech": "proptech",
    "healthtech": "healthtech",
    "saas": "cloud",
    "web3": "web3",
    "blockchain": "web3",
}


def user_domains(profile: dict) -> list[str]:
    skills = [str(item).strip().lower() for item in (profile or {}).get("skills") or [] if str(item).strip()]
    text = " ".join(
        skills
        + [
            str((profile or {}).get("headline") or ""),
            str((profile or {}).get("professional_summary") or ""),
            str((profile or {}).get("desired_title") or ""),
        ]
    ).lower()
    found: list[str] = []
    seen: set[str] = set()

    def add(domain: str) -> None:
        if domain and domain not in seen:
            seen.add(domain)
            found.append(domain)

    for skill in skills:
        add(_SHORT_SKILLS.get(skill, ""))
    for domain, phrases in _DOMAIN_PHRASES:
        if any(phrase in text for phrase in phrases):
            add(domain)
    return found


def user_location(profile: dict) -> str:
    profile = profile or {}
    return str(profile.get("city") or profile.get("state") or "").strip()


def event_matches_user(event: dict, profile: dict) -> bool:
    city = user_location(profile)
    domains = user_domains(profile)
    if not city or not domains:
        return False
    if not location_matches(event.get("city") or "", city):
        return False
    return any(domain_matches(event, domain) for domain in domains)


def reminder_message(event: dict) -> str:
    when = event.get("start_time") or ""
    place = event.get("location") or event.get("city") or ""
    clock = f"{when} · {place}" if when else place
    domains = ", ".join(event.get("domains") or [])
    lines = ["Event tomorrow", "", str(event.get("title") or "Tech event")]
    if clock:
        lines.append(clock)
    if domains:
        lines.append(domains)
    link = event.get("website") or event.get("url") or ""
    if link:
        lines.extend(["", link])
    return "\n".join(lines)


def notify_day_before(
    events: list[dict] | None = None,
    *,
    today: date | None = None,
    recipients: list[dict] | None = None,
    send=None,
    store_path: Path | None = None,
    use_mysql: bool = True,
) -> dict:
    """Send one WhatsApp per user for events that start tomorrow in Riyadh."""
    from jobscraper.whatsapp_alerts import alerts_enabled, alert_recipients, send_whatsapp

    if send is None and not alerts_enabled():
        return {"sent": 0, "skipped": "alerts disabled"}
    today = today or datetime.now(_TZ).date()
    tomorrow = (today + timedelta(days=1)).isoformat()
    rows = events if events is not None else load_events()
    due = [event for event in rows if str(event.get("start_date") or "") == tomorrow and event.get("id")]
    people = recipients if recipients is not None else alert_recipients()
    sender = send or send_whatsapp
    sent = 0
    considered = 0
    for event in due:
        for person in people:
            profile = person.get("profile") or {}
            phone = str(person.get("phone") or "")
            if not phone or not event_matches_user(event, profile):
                continue
            considered += 1
            if _already_sent(phone, event["id"], store_path, use_mysql):
                continue
            text = reminder_message(event)
            if not sender(phone, text):
                continue
            _mark_sent(
                phone,
                event,
                text,
                user_id=person.get("user_id"),
                store_path=store_path,
                use_mysql=use_mysql,
            )
            sent += 1
    log.info("Event reminders sent %s for %s event(s) tomorrow", sent, len(due))
    return {"sent": sent, "tomorrow": tomorrow, "events": len(due), "matches": considered}


def _sent_key(phone: str, event_id: str) -> str:
    return f"{phone}:{event_id}"


def _read_sent(path: Path | None) -> set[str]:
    target = path or SENT_FILE
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    rows = payload.get("sent") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return set()
    return {str(item) for item in rows if item}


def _write_sent(keys: set[str], path: Path | None) -> None:
    target = path or SENT_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"sent": sorted(keys)}, indent=2), encoding="utf-8")
    temporary.replace(target)


def _already_sent(phone: str, event_id: str, path: Path | None, use_mysql: bool) -> bool:
    if _sent_key(phone, event_id) in _read_sent(path):
        return True
    if not use_mysql:
        return False
    try:
        from jobscraper.db import get_conn, mysql_configured

        if not mysql_configured():
            return False
        conn = get_conn()
        if conn is None:
            return False
        with conn.cursor() as cur:
            cur.execute(CREATE_EVENT_ALERTS_SQL)
            cur.execute(
                f"SELECT id FROM {ALERTS_TABLE} WHERE phone=%s AND event_id=%s LIMIT 1",
                (phone, event_id),
            )
            return cur.fetchone() is not None
    except Exception:
        log.warning("Could not read event WhatsApp alerts", exc_info=True)
        return False


def _mark_sent(
    phone: str,
    event: dict,
    message: str,
    *,
    user_id: str | None,
    store_path: Path | None,
    use_mysql: bool,
) -> None:
    keys = _read_sent(store_path)
    keys.add(_sent_key(phone, event["id"]))
    _write_sent(keys, store_path)
    if not use_mysql:
        return
    try:
        from jobscraper.db import get_conn, mysql_configured

        if not mysql_configured():
            return
        conn = get_conn()
        if conn is None:
            return
        with conn.cursor() as cur:
            cur.execute(CREATE_EVENT_ALERTS_SQL)
            cur.execute(
                f"""
                INSERT INTO {ALERTS_TABLE} (
                  phone, event_id, user_id, title, city, start_date, message, sent_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6))
                ON DUPLICATE KEY UPDATE sent_at=VALUES(sent_at)
                """,
                (
                    phone,
                    event["id"],
                    user_id,
                    event.get("title") or "",
                    event.get("city") or "",
                    event.get("start_date") or None,
                    message,
                ),
            )
    except Exception:
        log.warning("Could not save event WhatsApp alert", exc_info=True)
