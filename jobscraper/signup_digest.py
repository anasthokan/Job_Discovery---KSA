"""Daily WhatsApp report of KSA platform signups.

At 20:00 Asia/Kolkata the scheduler counts candidate accounts created since
midnight IST and the total users on the platform, then texts that report.
Counts come from the KsaJob admin API when a token or admin login is set,
and from the local `users` table otherwise.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger("jobscraper.signup_digest")

_TZ = ZoneInfo("Asia/Kolkata")
_DEFAULT_NUMBER = "966563226748"
_IDENT = re.compile(r"^[A-Za-z0-9_]+$")
_CREATED = ("created_at", "date_joined", "registered_at", "created")
_ROLE = ("role", "user_type", "account_type", "user_role")
_NOT_CANDIDATES = {"employer", "company", "admin", "staff", "recruiter", "superadmin"}
_DATE_KEYS = (
    "created_at",
    "createdAt",
    "date_joined",
    "dateJoined",
    "registered_at",
    "registeredAt",
    "created",
)
_LIST_KEYS = ("results", "data", "items", "candidates", "users", "employers", "staff")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def digest_enabled() -> bool:
    return _env("SIGNUP_DIGEST_ENABLED", "true").lower() not in {"0", "false", "no", "off"}


def digest_hour() -> int:
    try:
        hour = int(_env("SIGNUP_DIGEST_HOUR", "20") or "20")
    except ValueError:
        hour = 20
    return hour if 0 <= hour <= 23 else 20


def digest_minute() -> int:
    try:
        minute = int(_env("SIGNUP_DIGEST_MINUTE", "0") or "0")
    except ValueError:
        minute = 0
    return minute if 0 <= minute <= 59 else 0


def digest_number() -> str:
    raw = _env("SIGNUP_DIGEST_WHATSAPP", _DEFAULT_NUMBER) or _DEFAULT_NUMBER
    from jobscraper.whatsapp_alerts import whatsapp_number

    return whatsapp_number(raw, "966") or _DEFAULT_NUMBER


def digest_status() -> dict:
    return {
        "enabled": digest_enabled(),
        "tz": "Asia/Kolkata",
        "at": f"{digest_hour():02d}:{digest_minute():02d}",
    }


def ist_day_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """UTC start and end of the current IST calendar day. End is exclusive."""
    current = now.astimezone(_TZ) if now is not None else datetime.now(_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_TZ)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return (
        start.astimezone(timezone.utc).replace(tzinfo=None),
        end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _ist_date(now: datetime | None = None) -> date:
    current = now.astimezone(_TZ) if now is not None else datetime.now(_TZ)
    return current.date()


def row_local_date(row: dict) -> date | None:
    """Calendar date in IST. Naive timestamps are treated as UTC."""
    if not isinstance(row, dict):
        return None
    value = None
    for key in _DATE_KEYS:
        if row.get(key) not in (None, ""):
            value = row.get(key)
            break
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        if len(text) >= 19 and text[10] == " ":
            text = text[:10] + "T" + text[11:]
        try:
            parsed = datetime.fromisoformat(text[:32])
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(_TZ).date()


def _page_rows(payload) -> tuple[list[dict], int | None]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)], len(payload)
    if not isinstance(payload, dict):
        return [], None
    rows: list[dict] = []
    for key in _LIST_KEYS:
        found = payload.get(key)
        if isinstance(found, list):
            rows = [row for row in found if isinstance(row, dict)]
            break
    total = payload.get("count")
    if total is None:
        total = payload.get("total")
    try:
        total_n = int(total) if total is not None else None
    except (TypeError, ValueError):
        total_n = None
    return rows, total_n


def tally_pages(pages: list, today: date) -> dict:
    """Sum one list endpoint. `today` stays None when rows have no dates."""
    total = None
    seen = 0
    dated = 0
    today_count = 0
    for payload in pages:
        rows, count = _page_rows(payload)
        if count is not None:
            total = count
        for row in rows:
            seen += 1
            stamp = row_local_date(row)
            if stamp is None:
                continue
            dated += 1
            if stamp == today:
                today_count += 1
        if rows and dated and all(
            (row_local_date(row) is not None and row_local_date(row) < today) for row in rows
        ):
            break
    if total is None:
        total = seen
    return {"total": total, "today": today_count if dated or total == 0 else None}


def digest_message(stats: dict) -> str:
    raw = str(stats.get("date") or "")
    try:
        label = datetime.strptime(raw, "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        label = raw or _ist_date().strftime("%d %b %Y")
    return "\n".join(
        [
            "KSA Jobs daily report",
            f"{label} (IST)",
            "",
            f"Candidates registered today: {int(stats.get('candidates_today') or 0)}",
            f"Total users on the platform: {int(stats.get('total_users') or 0)}",
        ]
    )


def _quote(name: str) -> str:
    if not _IDENT.fullmatch(name or ""):
        raise ValueError(f"Unsafe SQL name: {name}")
    return f"`{name}`"


def _table_columns(cur, table: str) -> set[str]:
    cur.execute(
        """
        SELECT COLUMN_NAME AS name
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
        """,
        (table,),
    )
    return {str(row.get("name") or "") for row in cur.fetchall() or []}


def _has_table(cur, table: str) -> bool:
    cur.execute("SHOW TABLES LIKE %s", (table,))
    return bool(cur.fetchone())


def _named(columns: set[str], choices: tuple[str, ...]) -> str:
    for name in choices:
        if name in columns:
            return name
    return ""


def _platform_conn():
    """RDP platform database when PLATFORM_MYSQL_HOST is set, otherwise the local app database."""
    host = _env("PLATFORM_MYSQL_HOST")
    if not host:
        from jobscraper.db import get_conn

        return get_conn(), False
    import pymysql

    try:
        port = int(_env("PLATFORM_MYSQL_PORT", "3306") or "3306")
    except ValueError:
        port = 3306
    conn = pymysql.connect(
        host=host,
        port=port,
        user=_env("PLATFORM_MYSQL_USER"),
        password=os.getenv("PLATFORM_MYSQL_PASSWORD") or "",
        database=_env("PLATFORM_MYSQL_DATABASE", "ksajob") or "ksajob",
        charset="utf8mb4",
        connect_timeout=8,
        read_timeout=20,
        cursorclass=pymysql.cursors.DictCursor,
    )
    return conn, True


def _mysql_counts(now: datetime | None = None) -> dict:
    start_utc, end_utc = ist_day_window(now)
    today = _ist_date(now)
    try:
        conn, owned = _platform_conn()
    except Exception as exc:
        log.exception("Signup digest database connection failed")
        return {"ok": False, "error": str(exc)[:200]}
    if conn is None:
        return {"ok": False, "error": "database is not configured"}
    try:
        with conn.cursor() as cur:
            if not _has_table(cur, "users"):
                return {"ok": False, "error": "users table is not in this database"}
            columns = _table_columns(cur, "users")
            created = _named(columns, _CREATED)
            if not created:
                return {"ok": False, "error": "users table has no created date"}
            role = _named(columns, _ROLE)
            cur.execute(f"SELECT COUNT(*) AS n FROM {_quote('users')}")
            total = int((cur.fetchone() or {}).get("n") or 0)
            sql = (
                f"SELECT COUNT(*) AS n FROM {_quote('users')} "
                f"WHERE {_quote(created)} >= %s AND {_quote(created)} < %s"
            )
            params: list = [start_utc, end_utc]
            if role:
                marks = ", ".join(["%s"] * len(_NOT_CANDIDATES))
                sql += f" AND LOWER(CAST({_quote(role)} AS CHAR)) NOT IN ({marks})"
                params.extend(sorted(_NOT_CANDIDATES))
            cur.execute(sql, params)
            registered = int((cur.fetchone() or {}).get("n") or 0)
    except Exception as exc:
        log.exception("Signup digest count failed")
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        if owned:
            conn.close()
    return {
        "ok": True,
        "source": "mysql",
        "date": today.isoformat(),
        "candidates_today": registered,
        "total_users": total,
    }


def _fetch_pages(path: str, fetch) -> list | None:
    pages = []
    for page in range(1, 41):
        payload = fetch(f"{path}?page={page}&page_size=100")
        if payload is None:
            return None if page == 1 else pages
        pages.append(payload)
        rows, total = _page_rows(payload)
        seen = sum(len(_page_rows(item)[0]) for item in pages)
        if not rows:
            break
        if total is not None and seen >= total:
            break
        nxt = payload.get("next") if isinstance(payload, dict) else None
        if not nxt:
            break
    return pages


def _api_counts(now: datetime | None = None) -> dict:
    from jobscraper.score_config import _access_token, _get_json

    today = _ist_date(now)
    if not _access_token():
        return {"ok": False, "error": "KsaJob admin login is not configured"}
    candidate_pages = _fetch_pages("/api/v1/admin/portal/candidates/", _get_json)
    if candidate_pages is None:
        return {"ok": False, "error": "KsaJob candidate list could not be read"}
    candidates = tally_pages(candidate_pages, today)
    total = int(candidates["total"] or 0)
    for path in (
        "/api/v1/admin/portal/employers/",
        "/api/v1/admin/portal/staff/",
    ):
        pages = _fetch_pages(path, _get_json)
        if pages is None:
            continue
        total += int(tally_pages(pages, today)["total"] or 0)
    registered = candidates.get("today")
    return {
        "ok": registered is not None,
        "source": "api",
        "date": today.isoformat(),
        "candidates_today": registered,
        "total_users": total,
        "error": None if registered is not None else "candidate list has no registration dates",
    }


def platform_counts(now: datetime | None = None) -> dict:
    """Candidates registered today (IST) and total platform users."""
    api = _api_counts(now)
    if api.get("ok") and api.get("candidates_today") is not None:
        return api
    db = _mysql_counts(now)
    if db.get("ok"):
        return db
    parts = [str(item) for item in (api.get("error"), db.get("error")) if item]
    return {
        "ok": False,
        "error": "; ".join(parts) or "could not count users",
        "date": _ist_date(now).isoformat(),
    }


def send_signup_digest(now: datetime | None = None, send=None) -> dict:
    """Text today's signup counts. Returns the counts and whether WhatsApp accepted them."""
    if not digest_enabled():
        return {"sent": False, "reason": "disabled"}
    stats = platform_counts(now)
    if not stats.get("ok"):
        log.warning("Signup digest skipped: %s", stats.get("error"))
        return {"sent": False, **stats}
    text = digest_message(stats)
    number = digest_number()
    if send is None:
        from jobscraper.whatsapp_alerts import send_whatsapp

        send = send_whatsapp
    try:
        sent = bool(send(number, text))
    except Exception:
        log.exception("Signup digest WhatsApp send failed")
        sent = False
    log.info(
        "Signup digest candidates_today=%s total_users=%s sent=%s",
        stats.get("candidates_today"),
        stats.get("total_users"),
        sent,
    )
    return {"sent": sent, "message": text, **stats}
