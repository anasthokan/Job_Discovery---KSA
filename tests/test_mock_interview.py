"""Mock interview generation without a live Ollama model."""

from __future__ import annotations

import json
import unittest

from fastapi import HTTPException

from jobscraper.mock_interview import MockInterviewError, start_interview, submit_answer
from jobscraper.ollama_resume import OllamaError


FACTS = {
    "profile": {"full_name": "Jane Doe", "location": "Riyadh"},
    "skills": ["Python", "FastAPI", "PostgreSQL"],
    "experience": [
        {
            "title": "Software Engineer",
            "company": "Acme Corp",
            "bullets": ["Built APIs used by 2 million users"],
        }
    ],
}

JOB = {"title": "Senior Backend Engineer", "company": "Northwind", "description": ""}


def _questions(count: int = 3) -> str:
    intro = {
        "category": "introduction",
        "prompt": "Tell me about yourself and why you want the Senior Backend Engineer role at Northwind.",
    }
    closing = {
        "category": "closing",
        "prompt": "What would you want to learn in your first 90 days in this role at Northwind?",
    }
    middle = [
        {"category": "experience", "prompt": "Walk me through your work as a Software Engineer at Acme Corp."},
        {"category": "technical", "prompt": "How have you used FastAPI to build a service other people depend on?"},
        {"category": "behavioral", "prompt": "Describe a time you disagreed with a teammate and what changed afterward."},
    ]
    rows = [intro, *middle[: max(count - 2, 0)], closing]
    return json.dumps({"questions": rows[:count]})


def _evaluation() -> str:
    return json.dumps(
        {
            "score": 8,
            "summary": "You named Acme Corp and the API work you did there.",
            "strengths": ["You named a real employer and a concrete result."],
            "improvements": ["Add what changed after the APIs launched."],
            "better_answer": "At Acme Corp I built FastAPI services used by 2 million users and kept them reliable.",
        }
    )


ANSWER = "At Acme Corp I built FastAPI services used by 2 million users and kept them reliable."


class MockInterviewTests(unittest.TestCase):
    def test_model_questions_start_with_introduction(self):
        seen = {}

        def writer(prompt):
            seen["prompt"] = prompt
            return _questions(5)

        built = start_interview(FACTS, job=JOB, question_count=5, writer=writer)
        self.assertEqual(built["question_source"], "model")
        self.assertEqual(built["status"], "in_progress")
        self.assertEqual(built["progress"], {"answered": 0, "total": 5})
        self.assertEqual(built["question"]["number"], 1)
        self.assertEqual(built["question"]["category"], "introduction")
        self.assertEqual(built["session"]["turns"], [])
        self.assertIn("Acme Corp", seen["prompt"])
        self.assertIn("Northwind", seen["prompt"])
        self.assertIn("FastAPI", seen["prompt"])

    def test_bad_json_uses_template_with_real_employer(self):
        built = start_interview(FACTS, job=JOB, question_count=5, writer=lambda _prompt: "not json")
        self.assertEqual(built["question_source"], "template")
        prompts = [item["prompt"] for item in built["session"]["questions"]]
        self.assertEqual(built["session"]["questions"][0]["category"], "introduction")
        self.assertEqual(built["session"]["questions"][-1]["category"], "closing")
        self.assertTrue(any("Acme Corp" in prompt for prompt in prompts))
        self.assertTrue(any("Northwind" in prompt for prompt in prompts))
        self.assertTrue(any("FastAPI" in prompt for prompt in prompts))

    def test_template_uses_project_when_there_is_no_employer(self):
        facts = {
            "skills": ["Python"],
            "projects": [{"name": "Campus Library", "description": "A search tool for the campus catalog."}],
        }
        built = start_interview(facts, job=JOB, writer=lambda _prompt: "not json")
        prompts = " ".join(item["prompt"] for item in built["session"]["questions"])
        self.assertIn("Campus Library", prompts)

    def test_missing_role(self):
        with self.assertRaises(MockInterviewError) as caught:
            start_interview(FACTS, writer=lambda _prompt: _questions())
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("job title", str(caught.exception))

    def test_bad_focus_and_count(self):
        with self.assertRaises(MockInterviewError) as caught:
            start_interview(FACTS, job=JOB, focus="casual", writer=lambda _prompt: _questions())
        self.assertEqual(caught.exception.status_code, 400)
        with self.assertRaises(MockInterviewError) as caught:
            start_interview(FACTS, job=JOB, question_count=2, writer=lambda _prompt: _questions())
        self.assertEqual(caught.exception.status_code, 400)

    def test_ollama_down(self):
        def _down(_prompt):
            raise OllamaError("Ollama is not reachable")

        with self.assertRaises(MockInterviewError) as caught:
            start_interview(FACTS, job=JOB, writer=_down)
        self.assertEqual(caught.exception.status_code, 503)

    def test_timeout_still_starts_with_template_questions(self):
        def _slow(_prompt):
            raise OllamaError("Ollama timed out")

        built = start_interview(FACTS, job=JOB, question_count=5, writer=_slow)
        self.assertEqual(built["question_source"], "template")
        self.assertEqual(built["status"], "in_progress")
        self.assertIn("did not finish in time", built["note"])
        self.assertTrue(built["question"]["prompt"])

    def test_timeout_on_answer_uses_rubric(self):
        started = start_interview(FACTS, job=JOB, question_count=3, writer=lambda _prompt: _questions(3))

        def _slow(_prompt):
            raise OllamaError("Ollama timed out")

        scored = submit_answer(started["session"], ANSWER, writer=_slow)
        self.assertEqual(scored["feedback"]["source"], "rubric")
        self.assertEqual(scored["question"]["number"], 2)

    def test_answer_advances_and_keeps_suggested_answer(self):
        started = start_interview(FACTS, job=JOB, question_count=3, writer=lambda _prompt: _questions(3))
        scored = submit_answer(started["session"], ANSWER, writer=lambda _prompt: _evaluation())
        self.assertEqual(scored["status"], "in_progress")
        self.assertEqual(scored["feedback"]["source"], "model")
        self.assertEqual(scored["feedback"]["score"], 8)
        self.assertIn("Acme Corp", scored["feedback"]["better_answer"])
        self.assertEqual(scored["question"]["number"], 2)
        self.assertEqual(scored["progress"]["answered"], 1)
        self.assertIsNone(scored["report"])

    def test_invented_number_drops_suggested_answer(self):
        started = start_interview(FACTS, job=JOB, question_count=3, writer=lambda _prompt: _questions(3))
        raw = json.dumps(
            {
                "score": 6,
                "summary": "You described the API work at Acme Corp.",
                "strengths": ["You named Acme Corp."],
                "improvements": ["Say what changed for the people using the API."],
                "better_answer": "At Acme Corp I cut latency by 40 percent and served 9 million extra users.",
            }
        )
        scored = submit_answer(started["session"], ANSWER, writer=lambda _prompt: raw)
        self.assertEqual(scored["feedback"]["source"], "model")
        self.assertIsNone(scored["feedback"]["better_answer"])

    def test_false_employment_uses_rubric(self):
        started = start_interview(FACTS, job=JOB, question_count=3, writer=lambda _prompt: _questions(3))
        raw = json.dumps(
            {
                "score": 8,
                "summary": "You already work at Northwind and know the backend well.",
                "strengths": ["You sounded confident about the team."],
                "improvements": ["Add an example from your own work history."],
                "better_answer": "I currently work at Northwind on the backend platform.",
            }
        )
        scored = submit_answer(started["session"], ANSWER, writer=lambda _prompt: raw)
        self.assertEqual(scored["feedback"]["source"], "rubric")
        self.assertNotIn("work at Northwind", scored["feedback"]["summary"])

    def test_bad_eval_json_uses_rubric_and_advances(self):
        started = start_interview(FACTS, job=JOB, question_count=3, writer=lambda _prompt: _questions(3))
        scored = submit_answer(started["session"], ANSWER, writer=lambda _prompt: "not json")
        self.assertEqual(scored["feedback"]["source"], "rubric")
        self.assertGreaterEqual(scored["feedback"]["score"], 5)
        self.assertEqual(scored["question"]["number"], 2)

    def test_last_answer_returns_report(self):
        started = start_interview(FACTS, job=JOB, question_count=3, writer=lambda _prompt: _questions(3))
        session = started["session"]
        result = None
        for _ in range(3):
            result = submit_answer(session, ANSWER, writer=lambda _prompt: _evaluation())
            session = result["session"]
        self.assertEqual(result["status"], "complete")
        self.assertIsNone(result["question"])
        self.assertEqual(result["report"]["overall_score"], 8)
        self.assertEqual(len(result["report"]["turns"]), 3)
        self.assertIn("Northwind", result["report"]["summary"])
        with self.assertRaises(MockInterviewError) as caught:
            submit_answer(session, ANSWER, writer=lambda _prompt: _evaluation())
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("already complete", str(caught.exception))

    def test_short_answer_and_missing_session(self):
        started = start_interview(FACTS, job=JOB, question_count=3, writer=lambda _prompt: _questions(3))
        with self.assertRaises(MockInterviewError) as caught:
            submit_answer(started["session"], "yes", writer=lambda _prompt: _evaluation())
        self.assertEqual(caught.exception.status_code, 400)
        with self.assertRaises(MockInterviewError) as caught:
            submit_answer({}, ANSWER, writer=lambda _prompt: _evaluation())
        self.assertEqual(caught.exception.status_code, 400)


class MockInterviewRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import api

        cls.api = api

    def test_contract_and_health_flag(self):
        body = self.api.mock_interview_contract()
        self.assertEqual(body["endpoints"]["start"]["path"], "/api/mock-interview/start")
        self.assertEqual(body["endpoints"]["answer"]["path"], "/api/mock-interview/answer")
        self.assertTrue(self.api.health()["mock_interview"])

    def test_empty_body_is_400(self):
        with self.assertRaises(HTTPException) as caught:
            self.api.start_mock_interview(self.api.MockInterviewStartRequest())
        self.assertEqual(caught.exception.status_code, 400)

    def test_bad_focus_is_400(self):
        body = self.api.MockInterviewStartRequest(
            target_title="Senior Backend Engineer",
            skills=["Python"],
            focus="casual",
        )
        with self.assertRaises(HTTPException) as caught:
            self.api.start_mock_interview(body)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("focus", caught.exception.detail)

    def test_start_route_passes_facts(self):
        def _fake(facts, **kwargs):
            self.assertEqual(facts["profile"]["full_name"], "Jane Doe")
            self.assertEqual(facts["skills"], ["Python"])
            self.assertEqual(kwargs["job"]["company"], "Northwind")
            self.assertEqual(kwargs["question_count"], 4)
            self.assertEqual(kwargs["focus"], "technical")
            return {"ok": True, "status": "in_progress", "question": {"number": 1, "prompt": "Why this role?"}}

        original = self.api.start_interview
        self.api.start_interview = _fake
        try:
            built = self.api.start_mock_interview(
                self.api.MockInterviewStartRequest(
                    full_name="Jane Doe",
                    skills=["Python"],
                    target_title="Senior Backend Engineer",
                    company="Northwind",
                    question_count=4,
                    focus="technical",
                )
            )
        finally:
            self.api.start_interview = original
        self.assertEqual(built["question"]["prompt"], "Why this role?")

    def test_answer_route_passes_session(self):
        def _fake(session, answer):
            self.assertEqual(session["id"], "abc")
            self.assertIn("Acme", answer)
            return {"ok": True, "status": "in_progress", "feedback": {"score": 7, "source": "model"}}

        original = self.api.submit_answer
        self.api.submit_answer = _fake
        try:
            built = self.api.answer_mock_interview(
                self.api.MockInterviewAnswerRequest(session={"id": "abc"}, answer="I built APIs at Acme Corp.")
            )
        finally:
            self.api.submit_answer = original
        self.assertEqual(built["feedback"]["score"], 7)


if __name__ == "__main__":
    unittest.main()
