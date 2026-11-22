"""IST signup counts and the daily WhatsApp text."""

from __future__ import annotations

import unittest
from datetime import date, datetime

from jobscraper.signup_digest import digest_message, row_local_date, tally_pages


TODAY = date(2026, 11, 22)


class DateTests(unittest.TestCase):
    def test_utc_evening_is_still_the_same_ist_day(self):
        self.assertEqual(row_local_date({"createdAt": "2026-11-22T18:29:00Z"}), TODAY)

    def test_utc_1830_is_the_next_ist_day(self):
        self.assertEqual(row_local_date({"createdAt": "2026-11-22T18:30:00Z"}), date(2026, 11, 23))

    def test_late_utc_previous_day_counts_as_today_in_ist(self):
        self.assertEqual(row_local_date({"date_joined": "2026-11-21T20:00:00+00:00"}), TODAY)

    def test_naive_mysql_datetime_is_utc(self):
        stamp = datetime(2026, 11, 22, 14, 0, tzinfo=None)
        self.assertEqual(row_local_date({"created_at": stamp}), TODAY)


class TallyTests(unittest.TestCase):
    def test_pages_count_today_and_stop_at_total(self):
        pages = [
            {
                "count": 3,
                "next": "page2",
                "results": [
                    {"createdAt": "2026-11-22T04:00:00Z"},
                    {"createdAt": "2026-11-21T12:00:00Z"},
                ],
            },
            {
                "count": 3,
                "next": None,
                "results": [{"createdAt": "2026-11-22T16:00:00Z"}],
            },
        ]
        tallied = tally_pages(pages, TODAY)
        self.assertEqual(tallied["total"], 3)
        self.assertEqual(tallied["today"], 2)


class MessageTests(unittest.TestCase):
    def test_report_names_both_counts(self):
        text = digest_message({"date": "2026-11-22", "candidates_today": 4, "total_users": 128})
        self.assertIn("22 Nov 2026 (IST)", text)
        self.assertIn("Candidates registered today: 4", text)
        self.assertIn("Total users on the platform: 128", text)
