from __future__ import annotations

import json
from datetime import timedelta

from duly.agent import ground, run_turn
from duly.gateway import TavilySearch
from duly.llm import SpendLedger
from duly.skills import load_skills
from duly.tools import Context, run_tool
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


def test_steps_the_skill_still_needs_are_reported_with_their_last_error(sample_store, cfg, tmp_path) -> None:
    def script(model, messages, kwargs):
        if "nano" in model.lower():
            return message(json.dumps({"skill": "warranty-claim"}))
        if not any(m.get("role") == "tool" for m in messages):
            return message(None, [("list_deadlines", {"within_days": 400}), ("web_search", {"query": "Dell claim"})])
        return message("Your laptop is still under warranty.")       # never searches again, never saves a draft

    no_credits = SpendLedger(tmp_path / "spend.json", 1.0, 1.0, daily_credits=0, total_credits=0)
    web = TavilySearch(cfg, client=object(), ledger=no_credits, cache_dir=tmp_path / "cache")
    llm, _ = fake_llm(cfg, script)
    ctx = Context(store=sample_store, today=TODAY, skills=load_skills(), web=web)
    turn = run_turn(ctx, llm, "My laptop screen flickers. Is it covered?", cfg)
    assert turn.answer == "Your laptop is still under warranty." and turn.error is None
    assert [s.label for s in turn.steps if s.kind == "tool"] == ["list_deadlines", "web_search", "skill check",
                                                                 "skill check"]
    assert turn.unfinished == ["save_draft", "web_search: web search unavailable: the demo's daily budget of 0 web "
                                             "searches is used up"]


def test_the_last_step_always_answers(sample_store, cfg) -> None:
    def script(model, messages, kwargs):
        if "nano" in model.lower():
            return message(json.dumps({"skill": "month-ahead"}))
        if "tools" not in kwargs:
            return message("Here is what I found.")
        if sum("Not finished" in (m.get("content") or "") for m in messages) < 2:
            return message("Nothing is due.")                            # skips list_deadlines, twice
        return message(None, [("search_documents", {"query": "gym"})])   # then one search per step

    llm, _ = fake_llm(cfg, script)
    turn = run_turn(Context(store=sample_store, today=TODAY, skills=load_skills()), llm, "What is due?", cfg)
    assert turn.answer == "Here is what I found." and turn.unfinished == ["list_deadlines"]
    assert sum(s.label == "search_documents" for s in turn.steps) == int(cfg["agent"]["max_tool_calls"]) - 1


def test_an_unexpected_error_ends_the_turn_with_a_message(sample_store, cfg, monkeypatch) -> None:
    def script(model, messages, kwargs):
        if "nano" in model.lower():
            return message(json.dumps({"skill": "none"}))
        if not any(m.get("role") == "tool" for m in messages):
            return message(None, [("search_documents", {"query": "gym renewal"})])
        return message("The gym renews soon.")

    def unplugged(*args, **kwargs):
        raise RuntimeError("disk unplugged")

    llm, _ = fake_llm(cfg, script)
    ctx = Context(store=sample_store, today=TODAY, skills=load_skills())
    with monkeypatch.context() as patch:                                 # a tool fails in the middle of the turn
        patch.setattr(sample_store, "search", unplugged)
        turn = run_turn(ctx, llm, "When does the gym renew?", cfg)
    assert turn.answer == "" and turn.error == "an unexpected error stopped this answer (RuntimeError: disk unplugged)"
    assert [s.label for s in turn.steps] == ["route", "step 1"]

    (sample_store.root / "notes.json").write_text("[{damaged", encoding="utf-8")   # fails before the first step
    turn = run_turn(ctx, llm, "When does the gym renew?", cfg)
    assert turn.answer == "" and "JSONDecodeError" in (turn.error or "") and turn.question_sent


def test_out_of_range_dates_are_errors_not_crashes(sample_store) -> None:
    ctx = Context(store=sample_store, today=TODAY, skills={})
    for args in ({"start_date": "9999-12-31", "add_days": 1}, {"start_date": "0001-01-01", "add_days": -1},
                 json.loads('{"start_date": "today", "add_days": 1e400}')):         # JSON reads 1e400 as infinity
        assert run_tool(ctx, "date_math", args)["error"].startswith("number or date out of range")
    far = run_tool(ctx, "date_math", {"start_date": "today", "add_days": 10 ** 12})
    assert far["iso"] == (TODAY + timedelta(days=36500)).isoformat() and "limited to 36500" in far["note"]
    back = run_tool(ctx, "date_math", {"start_date": "today", "add_days": -10 ** 12})
    assert back["iso"] == (TODAY - timedelta(days=36500)).isoformat()
    assert "error" in run_tool(ctx, "list_deadlines", json.loads('{"within_days": 1e400}'))
    assert "100 years" in run_tool(ctx, "add_reminder", {"title": "Never expires", "date": "9999-12-31"})["error"]
    assert not [o for o in sample_store.obligations() if o.title == "Never expires"]


def test_deadline_tool_gives_unambiguous_dates(sample_store) -> None:
    ctx = Context(store=sample_store, today=TODAY, skills={})
    out = run_tool(ctx, "list_deadlines", {"within_days": 400})
    gym = next(i for i in out["items"] if "gym" in i["title"].lower())
    assert gym["last_day_to_act"] == "Mon 9 Nov 2026 (in 10 days)" and gym["notice_days"] == 14
    search = run_tool(ctx, "search_documents", {"query": "laptop warranty"})
    laptop = next(r for r in search["results"] if r["document"] == "Laptop receipt and warranty")
    assert "2027-01-03 = Sun 3 Jan 2027 (in 65 days)" in laptop["dates_in_text"]


class _TavilyTwoSpellings:
    """Returns one news page under two host spellings, and the support page again under another spelling."""

    def search(self, query: str, **kwargs) -> dict:
        if "home" in query:
            return {"results": [{"url": "http://dell.com/support/home#top", "title": "Dell support", "content": "b"}]}
        page = "https://www.stocktitan.net/news/DELL/xps.html"
        return {"results": [{"url": page, "title": "XPS news", "content": "a"},
                            {"url": page.replace("www.", ""), "title": "XPS news", "content": "a"},
                            {"url": "https://www.dell.com/support/home/", "title": "Dell support", "content": "b"}]}


def test_a_page_found_under_two_spellings_is_listed_once(sample_store, cfg, tmp_path) -> None:
    web = TavilySearch(cfg, client=_TavilyTwoSpellings(), cache_dir=tmp_path / "cache")
    ctx = Context(store=sample_store, today=TODAY, skills={}, web=web)
    first = run_tool(ctx, "web_search", {"query": "Dell XPS 13 warranty claim support"})
    unique = ["https://www.stocktitan.net/news/DELL/xps.html", "https://www.dell.com/support/home/"]
    assert [r["url"] for r in first["results"]] == unique
    run_tool(ctx, "web_search", {"query": "Dell support home"})
    assert [s.url for s in ctx.sources] == unique
    again = web.search(sample_store, "Dell XPS 13 warranty claim support")      # from the cache
    assert [(s.url, s.via) for s in again] == [(u, "cached") for u in unique]


def test_citation_marks_from_the_model_are_removed_from_the_answer(sample_store, cfg) -> None:
    def script(model, messages, kwargs):
        if "nano" in model.lower():
            return message(json.dumps({"skill": "none"}))
        return message('Covered until Sun 3 Jan 2027 【{"id":"obl-162a1b","loc":0}】.\n'
                       '- Exclusions: accidental damage【{"id":0,"cursor":0,"loc":0}】 and spills【4†source】.')

    llm, _ = fake_llm(cfg, script)
    turn = run_turn(Context(store=sample_store, today=TODAY, skills=load_skills()), llm, "Am I covered?", cfg)
    assert turn.answer == "Covered until Sun 3 Jan 2027.\n- Exclusions: accidental damage and spills."
