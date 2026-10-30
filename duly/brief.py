"""The morning check: what needs attention today, run every morning without being asked.

Choosing what to report is plain code (dates and notice periods, no model). Nemotron Nano then writes a short,
friendly brief from the redacted list; if it is unavailable, a plain template is used, so the check works
offline too. On a computer it runs from a scheduled task (scripts/morning.py).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from duly.dates import describe
from duly.gateway import ask_model
from duly.llm import BudgetExceeded, LLMError, TokenFactory, utc_now
from duly.privacy import PrivacyError
from duly.store import KIND_LABELS, Store

Urgency = Literal["overdue", "today", "this week", "soon"]


class Alert(BaseModel):
    obligation_id: str
    title: str
    source: str
    act_by: date
    due: date
    urgency: Urgency
    amount: str | None = None
    action: str = ""
    auto_renews: bool | None = None


class Brief(BaseModel):
    day: date
    alerts: list[Alert]
    text: str                              # redacted; restored on screen
    written_by: str                        # model ID, or "template"
    cost_usd: float = 0.0
    at: str = Field(default_factory=utc_now)


def urgency_of(act_by: date, today: date, lead_days: int) -> Urgency:
    if act_by < today:
        return "overdue"
    if act_by == today:
        return "today"
    return "this week" if act_by <= today + timedelta(days=lead_days) else "soon"


def collect_alerts(store: Store, today: date, lead_days: int, window_days: int) -> list[Alert]:
    titles = {d.id: d.title for d in store.documents()}
    alerts = []
    for o in store.upcoming(today, window_days):
        alerts.append(Alert(obligation_id=o.id, title=o.title, source=o.counterparty or titles.get(o.doc_id or "", "your reminder"),
                            act_by=o.act_by, due=o.due_date, urgency=urgency_of(o.act_by, today, lead_days),
                            amount=f"{o.amount:,.0f} {o.currency or ''}".strip() if o.amount else None,
                            action=o.action or KIND_LABELS.get(o.kind, ""), auto_renews=o.auto_renews))
    return alerts


def template_text(alerts: list[Alert], today: date) -> str:
    if not alerts:
        return "Nothing needs your attention in the coming weeks."
    lines = []
    for a in alerts:
        lead = {"overdue": "Last day to act has passed", "today": "Today is the last day",
                "this week": "Act by", "soon": "Coming up"}[a.urgency]
        when = "" if a.urgency in ("overdue", "today") else f" {describe(a.act_by, today)}"
        money = f" ({a.amount})" if a.amount else ""
        lines.append(f"- {lead}{when}: {a.title}{money}. {a.action}".strip())
    return "\n".join(lines)


BRIEF_SYSTEM = """Write a short morning brief for the user from the list of deadlines below.
- At most 6 short lines, one per item, most urgent first, each like "Cancel the gym membership by Mon 9 Nov 2026
  (in 10 days)". Use the "act by" date written exactly as given, not the due date, and never a field name.
  Do not compute dates. Keep placeholders such as [PERSON_1] exactly as written.
- Mention when something renews automatically, since doing nothing means paying again.
- Plain text, no headings, no greeting longer than three words."""


def morning_check(store: Store, llm: TokenFactory | None, today: date, cfg: dict[str, Any]) -> Brief:
    lead = store.profile().lead_days or int(cfg["deadlines"]["lead_days"])
    alerts = collect_alerts(store, today, lead, int(cfg["deadlines"]["brief_window_days"]))
    brief = Brief(day=today, alerts=alerts, text=template_text(alerts, today), written_by="template")
    if alerts and llm is not None and llm.available:
        payload = [{"what": a.title, "from": a.source, "act by": describe(a.act_by, today),
                    "due": describe(a.due, today), "urgency": a.urgency, "amount": a.amount, "to do": a.action,
                    "renews automatically": a.auto_renews} for a in alerts]
        messages = [{"role": "system", "content": BRIEF_SYSTEM},
                    {"role": "user", "content": f"Today is {describe(today, today).split(' (')[0]}.\n"
                                                f"{json.dumps(payload, ensure_ascii=False, indent=1)}"}]
        try:
            res = ask_model(llm, store, "brief", messages, purpose="write the morning brief", thinking="off",
                            max_tokens=400)
            if res.content.strip():
                brief.text, brief.written_by, brief.cost_usd = res.content.strip(), res.call.model, res.call.cost_usd
        except (LLMError, BudgetExceeded, PrivacyError):
            pass
    store.save_brief(brief.model_dump(mode="json"))
    return brief
