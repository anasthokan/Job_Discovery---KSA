"""Day-before WhatsApp reminders follow city and domain."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from jobscraper.event_alerts import event_matches_user, notify_day_before, user_domains
from jobscraper.events import interval_hours


TODAY = date(2026, 11, 18)
TOMORROW = "2026-11-19"


def _event(city: str, domain: str, start: str, event_id: str = "evt-1") -> dict:
    return {
        "id": event_id,
        "title": f"{domain} meetup",
        "city": city,
        "domains": [domain],
        "category": "Meetup",
        "summary": "",
        "start_date": start,
        "start_time": "18:30",
        "end_time": "",
        "venue": "Hub",
        "location": f"Hub · {city}",
        "url": "https://example.com/event",
        "website": "",
    }


class MatchTests(unittest.TestCase):
    def test_ai_skill_and_riyadh_match(self):
        profile = {"city": "Riyadh", "skills": ["Python", "AI"]}
        self.assertIn("ai", user_domains(profile))
        self.assertTrue(event_matches_user(_event("Riyadh", "AI", TOMORROW), profile))

    def test_python_alone_is_not_a_domain_match(self):
        profile = {"city": "Riyadh", "skills": ["Python"]}
        self.assertFalse(event_matches_user(_event("Riyadh", "AI", TOMORROW), profile))

    def test_jeddah_user_does_not_match_riyadh(self):
        profile = {"city": "Jeddah", "skills": ["Fintech"]}
        self.assertFalse(event_matches_user(_event("Riyadh", "Fintech", TOMORROW), profile))


class NotifyTests(unittest.TestCase):
    def test_sends_once_on_the_day_before(self):
        sent: list[tuple[str, str]] = []
        person = {"phone": "966500000000", "user_id": "7", "profile": {"city": "Riyadh", "skills": ["AI"]}}
        events = [
            _event("Riyadh", "AI", TOMORROW, "tomorrow"),
            _event("Riyadh", "AI", "2026-11-18", "today"),
            _event("Riyadh", "Fintech", TOMORROW, "other-domain"),
            _event("Jeddah", "AI", TOMORROW, "other-city"),
        ]

        def send(phone: str, message: str) -> bool:
            sent.append((phone, message))
            return True

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sent.json"
            first = notify_day_before(
                events,
                today=TODAY,
                recipients=[person],
                send=send,
                store_path=path,
                use_mysql=False,
            )
            second = notify_day_before(
                events,
                today=TODAY,
                recipients=[person],
                send=send,
                store_path=path,
                use_mysql=False,
            )
        self.assertEqual(first["sent"], 1)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(len(sent), 1)
        self.assertIn("Event tomorrow", sent[0][1])
        self.assertIn("AI meetup", sent[0][1])

    def test_default_schedule_is_six_hours(self):
        self.assertEqual(interval_hours(), 6)


if __name__ == "__main__":
    unittest.main()
