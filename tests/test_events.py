"""Event scraping by location and domain, without the live sites."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from jobscraper.events import (
    EventsError,
    domain_matches,
    filter_events,
    location_matches,
    parse_date_label,
    parse_listing_html,
    delete_finished_events,
    event_is_finished,
    scrape_events,
)


TODAY = date(2026, 11, 18)

GUIDE_HTML = """
<html><body>
<p>Showing <!-- -->1<!-- -->–<!-- -->1<!-- --> of <!-- -->1<!-- --> events</p>
<p>Page <!-- -->1<!-- --> of <!-- -->1</p>
<article>
  <div><span class="text-foreground">18 Nov 2026</span><span class="text-muted-foreground"> · <!-- -->Today</span></div>
  <div class="mt-1">18:30</div>
  <h3><a href="/events/saudi-startup-majlis-4-proptech-2026">Saudi Startup Majlis #4 — PropTech Edition</a></h3>
  <p class="mt-1 text-sm text-muted-foreground">Saudi Venture Hub Office<!-- --> · <!-- -->Riyadh</p>
  <p class="mt-2 text-sm">PropTech and real-estate digitalisation founder/investor majlis.</p>
  <span class="label-xs inline-block border">Meetup</span>
  <span class="label-xs inline-block border">Paid</span>
  <a href="https://www.saudiventurehub.com/en/events/saudi-startup-majlis-4">Event website</a>
</article>
<article>
  <div><span class="text-foreground">21 Nov 2026</span><span class="text-muted-foreground"> · <!-- -->This weekend</span></div>
  <div class="mt-1">14:00–16:00</div>
  <h3><a href="/events/sabtiah-riyadh-ai-circle-2026-11-21">Sabtiah — Riyadh's AI Circle</a></h3>
  <p class="mt-1 text-sm text-muted-foreground">Sabtiah, Al Malqa<!-- --> · <!-- -->Riyadh</p>
  <p class="mt-2 text-sm">Weekly in-person AI builders circle.</p>
  <span class="label-xs inline-block border">Meetup</span>
  <span class="label-xs inline-block border">AI &amp; Data</span>
  <a href="https://sabtiah.cycls.com/">Event website</a>
</article>
</body></html>
"""

DIRECTORY_HTML = """
<html><body>
<p>Page <!-- -->1<!-- --> of <!-- -->1</p>
<article class="group">
  <div class="min-w-0">
    <p class="font-display text-2xl leading-tight">16–19 Nov</p>
    <p class="label-xs mt-2 text-muted-foreground">MON – THU</p>
    <p class="label-xs mt-1.5 flex items-center gap-1.5 text-primary"><span></span>Happening now</p>
  </div>
  <div class="min-w-0">
    <h3 class="font-display text-2xl leading-snug"><a href="/events/third-un-world-geospatial-information-congress-2026">Third United Nations World Geospatial Information Congress 2026</a></h3>
    <p class="label-xs mt-2.5 text-muted-foreground normal-case tracking-wide">The Ritz-Carlton Jeddah · Jeddah · Conference · Deep Tech · Free</p>
    <a href="https://www.un.org/globalgeospatial/en/events/2026/3unwgic">Event site ↗</a>
  </div>
</article>
<article class="group">
  <div class="min-w-0">
    <p class="font-display text-2xl leading-tight">24 Nov</p>
    <p class="label-xs mt-2 text-muted-foreground">TUE · 19:30</p>
  </div>
  <div class="min-w-0">
    <h3><a href="/events/majlis-ai-scientific-research-2026-11-24">Majlis.AI Gathering 12</a></h3>
    <p class="normal-case tracking-wide">Scitech · Al Khobar · Meetup · AI &amp; Data</p>
  </div>
</article>
</body></html>
"""

MIRSAD_HTML = """
<article class="event-card" data-section="events">
  <div class="event-datebox" aria-label="Nov 25, 2026, 6:00 – 9:00 PM"></div>
  <div class="event-kicker"><span>Majarah</span></div>
  <h2>Majarah AI Meetup</h2>
  <div class="event-details"><span>Nov 25, 2026, 6:00 – 9:00 PM</span><span>CODE, Riyadh</span></div>
  <p class="event-description">Community builders meetup.</p>
  <button class="tag">AI</button>
  <a class="event-link" href="https://www.eventbrite.com/e/majarah" data-event-id="eventbrite-riyadh-tech-rid8mq"></a>
  <span data-price="free"></span>
</article>
"""

SABTIAH_HTML = """
<html><body>
<a href="/sessions">Save your seat</a>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Event","name":"Sabtiah Saturday","startDate":"2026-11-21T14:00:00","endDate":"2026-11-21T16:00:00","url":"https://sabtiah.cycls.com/sessions/2026-11-21","location":{"@type":"Place","name":"Sabtiah, Al Malqa","address":{"addressLocality":"Riyadh"}},"description":"Weekly AI builders circle."}
</script>
</body></html>
"""


class ParseTests(unittest.TestCase):
    def test_guide_card_keeps_place_domain_and_time(self):
        events = parse_listing_html(GUIDE_HTML, "https://riyadhtechweek.com/events/riyadh", TODAY)
        majlis = events[0]
        self.assertEqual(majlis["title"], "Saudi Startup Majlis #4 — PropTech Edition")
        self.assertEqual(majlis["city"], "Riyadh")
        self.assertEqual(majlis["venue"], "Saudi Venture Hub Office")
        self.assertEqual(majlis["start_date"], "2026-11-18")
        self.assertEqual(majlis["start_time"], "18:30")
        self.assertEqual(majlis["category"], "Meetup")
        self.assertEqual(majlis["domains"], ["PropTech"])
        self.assertEqual(majlis["cost"], "paid")
        self.assertEqual(majlis["website"], "https://www.saudiventurehub.com/en/events/saudi-startup-majlis-4")
        self.assertEqual(majlis["source"], "Riyadh Tech Week")

    def test_directory_card_reads_city_and_domain(self):
        events = parse_listing_html(DIRECTORY_HTML, "https://riyadhtechweek.com/events", TODAY)
        jeddah = events[0]
        self.assertEqual(jeddah["city"], "Jeddah")
        self.assertEqual(jeddah["category"], "Conference")
        self.assertEqual(jeddah["domains"], ["Deep Tech"])
        self.assertEqual(jeddah["cost"], "free")
        self.assertEqual(jeddah["start_date"], "2026-11-16")
        self.assertEqual(jeddah["end_date"], "2026-11-19")
        khobar = events[1]
        self.assertEqual(khobar["city"], "Khobar")
        self.assertEqual(khobar["domains"], ["AI & Data"])
        self.assertEqual(khobar["start_time"], "19:30")
        self.assertEqual(khobar["start_date"], "2026-11-24")

    def test_cross_year_range_uses_the_slug_year(self):
        start, end = parse_date_label("31 Aug – 4 Feb", "innovate-4-impact-hackathon-2026", TODAY)
        self.assertEqual(start, "2026-08-31")
        self.assertEqual(end, "2027-02-04")


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.events = parse_listing_html(GUIDE_HTML, "https://riyadhtechweek.com/events/riyadh", TODAY)
        self.events += parse_listing_html(DIRECTORY_HTML, "https://riyadhtechweek.com/events", TODAY)

    def test_riyadh_proptech_excludes_jeddah_and_ai(self):
        matched = filter_events(self.events, "Riyadh", "PropTech")
        self.assertEqual([event["slug"] for event in matched], ["saudi-startup-majlis-4-proptech-2026"])

    def test_arabic_city_and_ai_domain(self):
        matched = filter_events(self.events, "الرياض", "AI")
        self.assertEqual(matched[0]["slug"], "sabtiah-riyadh-ai-circle-2026-11-21")

    def test_khobar_matches_eastern_province(self):
        matched = filter_events(self.events, "Eastern Province", "AI")
        self.assertEqual(matched[0]["city"], "Khobar")

    def test_domain_word_does_not_match_inside_another_word(self):
        event = {"title": "Saudi email summit", "domains": ["Fintech"], "category": "Conference", "summary": ""}
        self.assertFalse(domain_matches(event, "AI"))
        self.assertTrue(location_matches("Al Khobar", "khobar"))


class ScrapeTests(unittest.TestCase):
    def test_scrape_uses_riyadh_guide_and_sabtiah_for_ai(self):
        pages = {
            "https://riyadhtechweek.com/events": DIRECTORY_HTML,
            "https://riyadhtechweek.com/events/riyadh": GUIDE_HTML,
            "https://sabtiah.cycls.com": SABTIAH_HTML,
            "https://sabtiah.cycls.com/sessions": "<html><body><h1>Sessions</h1></body></html>",
            "https://www.mirsad.cc/": MIRSAD_HTML,
        }

        def fetch(url: str) -> str:
            if url not in pages:
                raise EventsError(f"unexpected {url}")
            return pages[url]

        with tempfile.TemporaryDirectory() as folder:
            report = scrape_events(
                "Riyadh",
                "AI",
                fetch=fetch,
                store_path=Path(folder) / "events.json",
                polite=False,
                today=TODAY,
            )
        titles = [event["title"] for event in report["events"]]
        self.assertIn("Sabtiah — Riyadh's AI Circle", titles)
        self.assertIn("Sabtiah Saturday", titles)
        self.assertIn("Majarah AI Meetup", titles)
        majarah = next(event for event in report["events"] if event["title"] == "Majarah AI Meetup")
        self.assertEqual(majarah["start_time"], "18:00")
        self.assertEqual(majarah["end_time"], "21:00")
        self.assertEqual(majarah["city"], "Riyadh")
        self.assertEqual(majarah["source"], "Mirsad")
        self.assertEqual(majarah["cost"], "free")
        self.assertNotIn("Saudi Startup Majlis #4 — PropTech Edition", titles)
        sources = {row["id"]: row for row in report["sources"]}
        self.assertTrue(sources["riyadhtechweek"]["ok"])
        self.assertTrue(sources["sabtiah"]["ok"])
        self.assertTrue(sources["mirsad"]["ok"])
        self.assertEqual(sources["riyadhtechweek"]["url"], "https://riyadhtechweek.com/events")
        self.assertIn("Jeddah", sources["riyadhtechweek"]["cities"])
        self.assertIn("Khobar", sources["riyadhtechweek"]["cities"])
        self.assertGreaterEqual(report["stored"], 4)

    def test_sabtiah_rsvp_page_keeps_the_upcoming_saturday(self):
        home = '<a href="https://liqa.io/sabtiah">Next session Save your seat</a>'
        liqa = (
            r'\"title\":\"السبتية - حلقة الرياض للذكاء الاصطناعي\",\"slug\":\"Y4DENJ8Z\",'
            r'\"startTimeStamp\":\"$D2026-11-21T11:00:00.000Z\",'
            r'\"endTimeStamp\":\"$D2026-11-21T13:00:00.000Z\",'
            r'\"timezone\":\"Asia/Riyadh\",\"location\":{\"city\":\"الملقا، الرياض\",\"isOnline\":false},'
            r'\"isPast\":false'
            r'\"title\":\"السبتية\",\"slug\":\"OLD\",'
            r'\"startTimeStamp\":\"$D2026-11-14T11:00:00.000Z\",'
            r'\"endTimeStamp\":\"$D2026-11-14T13:00:00.000Z\",'
            r'\"city\":\"الملقا، الرياض\",\"isPast\":true'
        )
        pages = {
            "https://sabtiah.cycls.com": home,
            "https://liqa.io/sabtiah": liqa,
        }

        def fetch(url: str) -> str:
            if url not in pages:
                raise EventsError(url)
            return pages[url]

        with tempfile.TemporaryDirectory() as folder:
            report = scrape_events(
                "Riyadh",
                "AI",
                fetch=fetch,
                store_path=Path(folder) / "events.json",
                polite=False,
                today=TODAY,
            )
        sabtiah = [event for event in report["events"] if event["source"] == "Sabtiah"]
        self.assertEqual(len(sabtiah), 1)
        self.assertEqual(sabtiah[0]["start_date"], "2026-11-21")
        self.assertEqual(sabtiah[0]["start_time"], "14:00")
        self.assertEqual(sabtiah[0]["end_time"], "16:00")
        self.assertEqual(sabtiah[0]["city"], "Riyadh")
        self.assertEqual(sabtiah[0]["venue"], "Sabtiah, Al Malqa")
        self.assertEqual(sabtiah[0]["url"], "https://liqa.io/events/Y4DENJ8Z")

    def test_jeddah_does_not_call_sabtiah(self):
        seen = []

        def fetch(url: str) -> str:
            seen.append(url)
            return DIRECTORY_HTML

        with tempfile.TemporaryDirectory() as folder:
            report = scrape_events(
                "Jeddah",
                "Deep Tech",
                fetch=fetch,
                store_path=Path(folder) / "events.json",
                polite=False,
                today=TODAY,
            )
        self.assertTrue(all("sabtiah" not in url and "mirsad" not in url for url in seen))
        self.assertEqual(report["events"][0]["city"], "Jeddah")

    def test_unknown_country_does_not_fetch(self):
        def fetch(url: str) -> str:
            raise AssertionError(url)

        report = scrape_events("Dubai", "AI", fetch=fetch, polite=False, today=TODAY)
        self.assertEqual(report["events"], [])
        self.assertIn("location", report["note"].lower())


class FinishedEventTests(unittest.TestCase):
    def test_yesterday_is_finished_and_today_stays(self):
        today = date(2026, 11, 19)
        done = {"start_date": "2026-11-18", "end_date": "2026-11-18"}
        still_on = {"start_date": "2026-11-19", "end_date": "2026-11-19"}
        spanning = {"start_date": "2026-11-18", "end_date": "2026-11-21"}
        self.assertTrue(event_is_finished(done, today))
        self.assertFalse(event_is_finished(still_on, today))
        self.assertFalse(event_is_finished(spanning, today))

    def test_finished_events_are_removed_from_the_saved_calendar(self):
        today = date(2026, 11, 19)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.json"
            path.write_text(
                json.dumps(
                    {
                        "updated_at": "2026-11-18T00:00:00+00:00",
                        "events": [
                            {"id": "past", "title": "Done", "start_date": "2026-11-18", "end_date": "2026-11-18"},
                            {"id": "live", "title": "Today", "start_date": "2026-11-19", "end_date": "2026-11-19"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            removed = delete_finished_events(today, path)
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(removed, 1)
        self.assertEqual([event["id"] for event in saved["events"]], ["live"])


if __name__ == "__main__":
    unittest.main()
