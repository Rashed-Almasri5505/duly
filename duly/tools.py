"""The assistant's tools. Each one works on the user's local data and returns only redacted text.

The model never computes dates and never sends anything: dates come from list_deadlines and date_math,
and drafts are saved for the user to review and send themselves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from duly.dates import describe, find_dates, slash_convention
from duly.gateway import LLMError, PrivacyError, Source, TavilySearch
from duly.llm import BudgetExceeded
from duly.skills import Skill
from duly.store import KIND_LABELS, Store


@dataclass
class Context:
    """What one assistant turn may use, and what it produced (for the UI)."""

    store: Store
    today: date
    skills: dict[str, Skill]
    web: TavilySearch | None = None
    allow_web: bool = True
    sources: list[Source] = field(default_factory=list)
    drafts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reminders: list[str] = field(default_factory=list)
    skills_used: list[str] = field(default_factory=list)


def tool_schemas(skill_names: list[str]) -> list[dict[str, Any]]:
    def fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {"type": "function", "function": {"name": name, "description": description, "parameters": {
            "type": "object", "properties": properties, "required": required}}}

    return [
        fn("list_deadlines", "Dated obligations from the user's documents and reminders, ordered by the last day to "
           "act. Each item gives the due date, the last day to act (due date minus any notice period), the "
           "amount, what to do, and the document's own words.",
           {"within_days": {"type": "integer", "description": "how many days ahead to look (default 60)"}}, []),
        fn("search_documents", "Search the text of the user's documents and remembered facts. Use it to read terms, "
           "conditions, exclusions and procedures written in the documents.",
           {"query": {"type": "string"}}, ["query"]),
        fn("use_skill", "Load the step-by-step procedure of a skill before doing that kind of task.",
           {"name": {"type": "string", "enum": skill_names}}, ["name"]),
        fn("web_search", "Search the web (Tavily) for official procedures of companies and authorities: how to "
           "claim, renew, cancel or pay. Never put personal details or placeholders in the query.",
           {"query": {"type": "string"}}, ["query"]),
        fn("date_math", "Date arithmetic. Gives start_date plus add_days, or the days from start_date to end_date. "
           "Dates are YYYY-MM-DD or 'today'.",
           {"start_date": {"type": "string"}, "add_days": {"type": "integer"}, "end_date": {"type": "string"}},
           ["start_date"]),
        fn("save_draft", "Save an e-mail, letter or checklist for the user to review and send themselves. "
           "Write placeholders such as [PERSON_1] exactly as they appear; the app fills in the real values.",
           {"kind": {"type": "string", "enum": ["email", "letter", "checklist"]}, "title": {"type": "string"},
            "to": {"type": "string", "description": "recipient, may be a placeholder like [EMAIL_2]"},
            "body": {"type": "string"}}, ["kind", "title", "body"]),
        fn("add_reminder", "Add a dated reminder to the user's deadlines.",
           {"title": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD"},
            "note": {"type": "string"}}, ["title", "date"]),
        fn("remember", "Remember a lasting fact or preference the user stated about themselves.",
           {"fact": {"type": "string"}}, ["fact"]),
        fn("forget", "Forget a remembered fact, by its id.", {"note_id": {"type": "string"}}, ["note_id"]),
    ]


def _parse_day(text: str, today: date) -> date:
    return today if str(text).strip().lower() == "today" else date.fromisoformat(str(text).strip()[:10])


def list_deadlines(ctx: Context, within_days: int = 60) -> dict[str, Any]:
    within_days = max(1, min(int(within_days or 60), 800))
    titles = {d.id: d.title for d in ctx.store.documents()}
    items = []
    for o in ctx.store.upcoming(ctx.today, within_days):
        amount = f"{o.amount:,.0f} {o.currency or ''}".strip() if o.amount else None
        items.append({
            "id": o.id, "title": o.title, "type": KIND_LABELS.get(o.kind, "Deadline"),
            "from": o.counterparty or titles.get(o.doc_id or "", "your reminder"),
            "document": titles.get(o.doc_id or "") if o.doc_id else "reminder you added",
            "due": describe(o.due_date, ctx.today), "last_day_to_act": describe(o.act_by, ctx.today),
            "notice_days": o.notice_days, "amount": amount, "renews_automatically": o.auto_renews,
            "what_to_do": o.action or None, "document_says": o.date_quote, "notice_rule": o.rule_quote,
            "checked": "date and notice period found in the document" if o.verified else f"not confirmed: {o.check_note}",
        })
    return {"today": describe(ctx.today, ctx.today), "looking_ahead_days": within_days, "items": items}


def run_tool(ctx: Context, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one tool. Errors come back as {"error": ...} so the model can recover."""
    try:
        if name == "list_deadlines":
            return list_deadlines(ctx, args.get("within_days", 60))
        if name == "search_documents":
            hits = ctx.store.search(str(args.get("query", "")), k=5)
            docs = {d.id: d for d in ctx.store.documents()}
            results = []
            for h in hits:
                item: dict[str, Any] = {"document": h["source"], "text": h["text"]}
                doc = docs.get(h["doc_id"])
                # Numeric dates are ambiguous (03/01/2027); code reads them with the document's own convention.
                dates = find_dates(h["text"], slash_convention(doc.redacted) if doc else None)
                if dates:
                    item["dates_in_text"] = [f"{d.isoformat()} = {describe(d, ctx.today)}" for d, _ in dates]
                results.append(item)
            return {"results": results, "note": None if hits else "nothing in the documents matches"}
        if name == "use_skill":
            skill = ctx.skills.get(str(args.get("name")))
            if skill is None:
                return {"error": f"no skill named {args.get('name')!r}; available: {', '.join(ctx.skills)}"}
            if skill.name not in ctx.skills_used:
                ctx.skills_used.append(skill.name)
            return {"skill": skill.name, "steps": skill.body}
        if name == "web_search":
            if not ctx.allow_web:
                return {"error": "web search is switched off in the privacy settings; answer from the documents"}
            if ctx.web is None or not ctx.web.available:
                return {"error": "web search is not configured on this installation"}
            query = " ".join(str(args.get("query", "")).split())[:300]
            sources = ctx.web.search(ctx.store, query, purpose="web search by the assistant")
            ctx.sources.extend(s for s in sources if s.url not in {x.url for x in ctx.sources})
            return {"results": [{"title": s.title, "url": s.url, "snippet": s.snippet, "via": s.label}
                                for s in sources]}
        if name == "date_math":
            start = _parse_day(args.get("start_date", "today"), ctx.today)
            if args.get("end_date"):
                end = _parse_day(args["end_date"], ctx.today)
                return {"from": describe(start, ctx.today), "to": describe(end, ctx.today), "days": (end - start).days}
            result = start + timedelta(days=int(args.get("add_days") or 0))
            return {"result": describe(result, ctx.today), "iso": result.isoformat()}
        if name == "save_draft":
            draft = ctx.store.add_draft(str(args.get("title", "Draft"))[:120], str(args.get("body", "")),
                                        kind=str(args.get("kind", "email")), to=args.get("to"))
            ctx.drafts.append(draft.id)
            return {"draft_id": draft.id, "saved": True, "note": "Saved for the user to review. Nothing was sent."}
        if name == "add_reminder":
            when = _parse_day(args.get("date", ""), ctx.today)
            if when < ctx.today:
                return {"error": f"{when.isoformat()} is in the past"}
            item = ctx.store.add_reminder(str(args.get("title", "Reminder"))[:120], when, str(args.get("note", "")))
            ctx.reminders.append(item.id)
            return {"reminder_id": item.id, "date": describe(when, ctx.today)}
        if name == "remember":
            note = ctx.store.add_note(str(args.get("fact", ""))[:400])
            ctx.notes.append(note.id)
            return {"note_id": note.id, "saved": True}
        if name == "forget":
            return {"forgotten": ctx.store.forget_note(str(args.get("note_id", "")))}
        return {"error": f"unknown tool {name!r}"}
    except PrivacyError as exc:
        return {"error": f"{exc}. Search again without personal details."}
    except BudgetExceeded as exc:
        return {"error": f"web search unavailable: {exc}"}
    except LLMError as exc:
        return {"error": str(exc)}
    except (ValueError, TypeError) as exc:
        return {"error": f"bad arguments: {exc}"}


def summarize_result(name: str, result: dict[str, Any]) -> str:
    """One line for the step timeline."""
    if "error" in result:
        return f"error: {result['error']}"
    if name == "list_deadlines":
        return f"{len(result['items'])} items in the next {result['looking_ahead_days']} days"
    if name == "search_documents":
        return f"{len(result['results'])} passages" + (f" ({', '.join(sorted({r['document'] for r in result['results']}))})"
                                                      if result["results"] else "")
    if name == "use_skill":
        return f"loaded skill {result['skill']}"
    if name == "web_search":
        return f"{len(result['results'])} sources" + (f", first: {result['results'][0]['url']}" if result["results"] else "")
    if name == "date_math":
        return result.get("result") or f"{result.get('days')} days"
    if name == "save_draft":
        return f"draft {result['draft_id']} saved"
    if name == "add_reminder":
        return f"reminder for {result['date']}"
    if name == "remember":
        return f"remembered as {result['note_id']}"
    if name == "forget":
        return "forgotten" if result.get("forgotten") else "no such note"
    return json.dumps(result)[:120]
