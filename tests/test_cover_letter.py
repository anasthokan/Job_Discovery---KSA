"""Cover letter generation without a live Ollama model."""

from __future__ import annotations

import json
import unittest

from fastapi import HTTPException

from jobscraper.cover_letter import CoverLetterError, generate_cover_letter
from jobscraper.ollama_resume import OllamaError


FACTS = {
    "profile": {
        "full_name": "Jane Doe",
        "email": "jane@example.com",
        "location": "Riyadh",
    },
    "skills": ["Python", "FastAPI", "PostgreSQL"],
    "experience": [
        {
            "title": "Software Engineer",
            "company": "Acme Corp",
            "start_date": "Jan 2022",
            "end_date": "Present",
            "bullets": ["Built APIs used by 2 million users"],
        }
    ],
}

JOB = {"title": "Senior Backend Engineer", "company": "Northwind", "description": ""}


def _paragraphs() -> str:
    return json.dumps(
        {
            "paragraphs": [
                "I am writing to apply for the Senior Backend Engineer role at Northwind. My work uses Python, FastAPI, and PostgreSQL.",
                "As a Software Engineer at Acme Corp, I built APIs used by 2 million users and kept the service reliable.",
                "I would welcome the chance to discuss how this experience fits the Senior Backend Engineer opening at Northwind.",
            ]
        }
    )


class CoverLetterTests(unittest.TestCase):
    def test_model_letter_keeps_employer_and_greeting(self):
        built = generate_cover_letter(
            FACTS,
            job=JOB,
            hiring_manager="Sara Ali",
            writer=lambda _prompt: _paragraphs(),
        )
        self.assertEqual(built["letter_source"], "model")
        self.assertEqual(built["letter"]["greeting"], "Dear Sara Ali,")
        self.assertEqual(built["letter"]["signature"], "Jane Doe")
        self.assertEqual(len(built["letter"]["paragraphs"]), 3)
        self.assertIn("Acme Corp", built["letter_text"])
        self.assertIn("Northwind", built["letter_text"])
        self.assertNotIn("Dear Hiring Manager", built["letter_text"])

    def test_new_number_falls_back_to_facts(self):
        raw = json.dumps(
            {
                "paragraphs": [
                    "I am writing to apply for the Senior Backend Engineer role at Northwind with 15 years of leadership.",
                    "I increased revenue by 99 percent while working as a Software Engineer at Acme Corp.",
                    "I would welcome a conversation about the Senior Backend Engineer opening at Northwind this week.",
                ]
            }
        )
        built = generate_cover_letter(FACTS, job=JOB, writer=lambda _prompt: raw)
        self.assertEqual(built["letter_source"], "facts")
        self.assertNotIn("99", built["letter_text"])
        self.assertIn("Acme Corp", built["letter_text"])
        self.assertIn("2 million", built["letter_text"])

    def test_false_employment_claim_falls_back(self):
        raw = json.dumps(
            {
                "paragraphs": [
                    "I am writing to apply for the Senior Backend Engineer role at Northwind and can start soon.",
                    "I currently work at Northwind as a staff engineer building internal platforms.",
                    "I would welcome the chance to discuss the Senior Backend Engineer opening with your team.",
                ]
            }
        )
        built = generate_cover_letter(FACTS, job=JOB, writer=lambda _prompt: raw)
        self.assertEqual(built["letter_source"], "facts")
        self.assertNotIn("currently work at Northwind", built["letter_text"])

    def test_missing_facts(self):
        with self.assertRaises(CoverLetterError) as caught:
            generate_cover_letter({}, job=JOB, writer=lambda _prompt: _paragraphs())
        self.assertEqual(caught.exception.status_code, 400)

    def test_missing_job(self):
        with self.assertRaises(CoverLetterError) as caught:
            generate_cover_letter(FACTS, writer=lambda _prompt: _paragraphs())
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("job title", str(caught.exception))

    def test_ollama_down(self):
        def _down(_prompt):
            raise OllamaError("Ollama is not reachable")

        with self.assertRaises(CoverLetterError) as caught:
            generate_cover_letter(FACTS, job=JOB, writer=_down)
        self.assertEqual(caught.exception.status_code, 503)

    def test_invalid_json(self):
        with self.assertRaises(CoverLetterError) as caught:
            generate_cover_letter(FACTS, job=JOB, writer=lambda _prompt: "not json")
        self.assertEqual(caught.exception.status_code, 502)


class CoverLetterRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import api

        cls.api = api

    def test_contract(self):
        body = self.api.cover_letter_contract()
        self.assertEqual(body["endpoints"]["generate"]["path"], "/api/cover-letter/generate")

    def test_empty_body_is_400(self):
        with self.assertRaises(HTTPException) as caught:
            self.api.generate_ai_cover_letter(self.api.CoverLetterRequest(), "json")
        self.assertEqual(caught.exception.status_code, 400)

    def test_skills_without_job_is_400(self):
        body = self.api.CoverLetterRequest(full_name="Jane Doe", skills=["Python"])
        with self.assertRaises(HTTPException) as caught:
            self.api.generate_ai_cover_letter(body, "json")
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("job title", caught.exception.detail)

    def test_bad_format(self):
        body = self.api.CoverLetterRequest(
            full_name="Jane Doe",
            skills=["Python"],
            target_title="Engineer",
        )
        with self.assertRaises(HTTPException) as caught:
            self.api.generate_ai_cover_letter(body, "html")
        self.assertEqual(caught.exception.status_code, 400)

    def test_json_route_uses_generator(self):
        def _fake(facts, **kwargs):
            self.assertEqual(facts["profile"]["full_name"], "Jane Doe")
            self.assertEqual(kwargs["hiring_manager"], "Sara Ali")
            self.assertEqual(kwargs["job"]["company"], "Northwind")
            return {
                "ok": True,
                "mode": "generated",
                "model": "test",
                "letter_source": "model",
                "letter_text": "Jane Doe\n",
                "letter": {"greeting": "Dear Sara Ali,", "paragraphs": ["a", "b", "c"]},
            }

        original = self.api.generate_cover_letter
        self.api.generate_cover_letter = _fake
        try:
            built = self.api.generate_ai_cover_letter(
                self.api.CoverLetterRequest(
                    full_name="Jane Doe",
                    skills=["Python"],
                    target_title="Senior Backend Engineer",
                    company="Northwind",
                    hiring_manager="Sara Ali",
                ),
                "json",
            )
        finally:
            self.api.generate_cover_letter = original
        self.assertEqual(built["letter"]["greeting"], "Dear Sara Ali,")

    def test_pdf_route_downloads_cover_letter(self):
        letter = (
            "Jane Doe\n"
            "Riyadh | jane@example.com\n"
            "\n"
            "Dear Sara Ali,\n"
            "\n"
            "I am writing to apply for the Senior Backend Engineer role at Northwind.\n"
            "\n"
            "I worked as a Software Engineer at Acme Corp.\n"
            "\n"
            "Thank you for considering my application.\n"
            "\n"
            "Sincerely,\n"
            "Jane Doe\n"
        )

        def _fake(_facts, **_kwargs):
            return {
                "ok": True,
                "mode": "generated",
                "model": "test",
                "letter_source": "facts",
                "letter_text": letter,
            }

        original = self.api.generate_cover_letter
        self.api.generate_cover_letter = _fake
        try:
            response = self.api.generate_ai_cover_letter(
                self.api.CoverLetterRequest(
                    full_name="Jane Doe",
                    skills=["Python"],
                    company="Northwind",
                ),
                "pdf",
            )
        finally:
            self.api.generate_cover_letter = original
        self.assertEqual(response.media_type, "application/pdf")
        self.assertIn("cover-letter.pdf", response.headers["content-disposition"])
        self.assertEqual(response.headers["x-cover-letter-source"], "facts")
        self.assertTrue(response.body.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
