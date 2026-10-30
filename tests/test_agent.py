from __future__ import annotations

import json

from duly.agent import ground, run_turn
from duly.skills import load_skills
from duly.tools import Context
from tests.conftest import TODAY, fake_llm, message


def test_links_and_addresses_must_come_from_a_tool_result() -> None:
    evidence = json.dumps({"results": [{"url": "https://www.dell.com/support/home/en-us"}],
                           "text": "Send notice to memberships@pulsefitness.example"})
    answer = ("See https://www.dell.com/support/home/en-us and [the form](https://dell.example/claim). "
              "Write to support@dell.com or memberships@pulsefitness.example.")
    out, removed = ground(answer, evidence)
    assert "https://www.dell.com/support/home/en-us" in out and "memberships@pulsefitness.example" in out
    assert "dell.example/claim" not in out and "support@dell.com" not in out
    assert sorted(removed) == ["https://dell.example/claim", "support@dell.com"]


def test_a_skipped_skill_step_is_asked_for_and_the_question_is_redacted(sample_store, cfg) -> None:
    def script(model, messages, kwargs):
        if "nano" in model.lower():                                       # the router
            return message(json.dumps({"skill": "month-ahead"}))
        tools_called = [m for m in messages if m.get("role") == "tool"]
        if not tools_called and not any("Not finished" in (m.get("content") or "") for m in messages):
            return message("Nothing much is due.")                       # skips the required list_deadlines
        if not tools_called:
            return message(None, [("list_deadlines", {"within_days": 31})])
        return message("Cancel the gym by Mon 9 Nov 2026.")

    llm, client = fake_llm(cfg, script)
    ctx = Context(store=sample_store, today=TODAY, skills=load_skills())
    turn = run_turn(ctx, llm, "I am Sami Nasser, phone 0505550134. What is due?", cfg)
    assert turn.answer == "Cancel the gym by Mon 9 Nov 2026."
    assert [s.label for s in turn.steps if s.kind == "tool"] == ["skill check", "list_deadlines"]
    assert turn.skills_used == ["month-ahead"]
    for request in client.requests:
        sent = json.dumps(request["messages"])
        assert "Sami" not in sent and "0505550134" not in sent
    assert "[PERSON_1]" in turn.question_sent


def test_deadline_tool_gives_unambiguous_dates(sample_store) -> None:
    from duly.tools import run_tool
    ctx = Context(store=sample_store, today=TODAY, skills={})
    out = run_tool(ctx, "list_deadlines", {"within_days": 400})
    gym = next(i for i in out["items"] if "gym" in i["title"].lower())
    assert gym["last_day_to_act"] == "Mon 9 Nov 2026 (in 10 days)" and gym["notice_days"] == 14
    search = run_tool(ctx, "search_documents", {"query": "laptop warranty"})
    laptop = next(r for r in search["results"] if r["document"] == "Laptop receipt and warranty")
    assert "2027-01-03 = Sun 3 Jan 2027 (in 65 days)" in laptop["dates_in_text"]
