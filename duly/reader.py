"""Reads a document into dated obligations: renewals, expiries, payments, notice periods, warranty ends.

The model never reads a date out of the document itself. Code first finds every date written in the redacted
document and numbers them (D1, D2 ...). Nemotron Super then says which of those dates matter and what each one
means, choosing from the numbered list in a strict JSON schema. The quote for each date is its line in the
document, and a notice period is marked "check this" unless code finds the number of days in a sentence of the
document. Anything that fails a check is kept but marked, so a misread is never presented as confirmed.

Measured on 2026-10-30, before this design: when Super wrote dates itself, it read 03/01/2027 as 1 March,
and subtracted a notice period from a date that already had it applied. The checks flagged both.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from duly.dates import find_dates, number_in_text, quote_in_text
from duly.gateway import ask_model
from duly.llm import TokenFactory
from duly.store import Document, Obligation, Store, new_id

KINDS = ["renewal", "expiry", "payment", "cancel_or_notice", "warranty_end", "return_window", "other"]


def read_schema(date_ids: list[str]) -> dict[str, Any]:
    return {
        "name": "document_obligations",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "obligations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date_id": {"type": "string", "enum": date_ids},
                            "kind": {"type": "string", "enum": KINDS},
                            "title": {"type": "string", "description": "short, e.g. 'Gym membership renews'"},
                            "notice_days": {"type": "integer",
                                            "description": "days before this date the owner must act, or 0"},
                            "rule_quote": {"type": ["string", "null"],
                                           "description": "exact words stating the notice period, or null"},
                            "amount": {"type": ["number", "null"]},
                            "currency": {"type": ["string", "null"]},
                            "auto_renews": {"type": ["boolean", "null"]},
                            "action": {"type": "string", "description": "what the owner must do, one sentence"},
                            "status": {"type": "string", "enum": ["upcoming", "done"]},
                        },
                        "required": ["date_id", "kind", "title", "notice_days", "rule_quote", "amount", "currency",
                                     "auto_renews", "action", "status"],
                        "additionalProperties": False,
                    },
                },
                "counterparty": {"type": ["string", "null"],
                                 "description": "the company or office on the other side"},
                "summary": {"type": "string", "description": "one sentence: what this document is"},
            },
            "required": ["obligations", "counterparty", "summary"],
            "additionalProperties": False,
        },
    }


SYSTEM = """You read one personal document for a paperwork assistant.
Personal details were replaced by placeholders such as [PERSON_1] or [ACCOUNT_2]; copy them exactly if you quote.

The dates written in the document are listed with an ID (D1, D2 ...). Pick the dates the owner must act on or
know about, and say what each one means:
- renewals and policy expiries, payments due, the end of a contract, lease or membership term,
  warranty end dates, return windows that end on that date;
- skip dates that are only history (a purchase, a signing date, a start date) and payments marked as paid.

For each date you pick:
- notice_days: if the document says to act a number of days before that date (cancel, give notice, contact us
  to renew), that number; otherwise 0. Do not change the date.
- rule_quote: copy, word for word, the sentence that states that notice period, or null.
- status: "done" only if the document marks this item as paid or completed; otherwise "upcoming".
- action: one plain sentence telling the owner what to do.
A date may appear in two items only if it means two different things.
summary: one plain sentence saying what the document is and who the other party is.

Answer with JSON only, in exactly this shape:
{"obligations": [{"date_id": "D2", "kind": "renewal", "title": "Gym membership renews", "notice_days": 14,
  "rule_quote": "unless the member gives written notice of cancellation at least 14 days before the end of the term.",
  "amount": 3600, "currency": "SAR", "auto_renews": true,
  "action": "Give written notice before the last day if you do not want to renew.", "status": "upcoming"}],
 "counterparty": "Pulse Fitness Club", "summary": "Gym membership agreement with Pulse Fitness Club."}
kind is one of: renewal, expiry, payment, cancel_or_notice, warranty_end, return_window, other."""


# Measured on 2026-10-30: the model's reasoning does not see the JSON schema (only the decoder does), so without
# the shape above it sometimes planned other keys and the constrained answer came out with an empty list.


def _parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def _sentences(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.strip(" -*|\t")
        if line:
            out.append(line)
            out.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+", line) if s.strip() and s.strip() != line)
    return out


def find_rule_quote(days: int, text: str) -> str | None:
    """The shortest sentence of the document that states a period of this many days before something."""
    hits = [s for s in _sentences(text) if number_in_text(days, s) and re.search(r"\bdays?\b", s, re.IGNORECASE)
            and re.search(r"\b(before|notice|prior|in advance|cancel|renew)", s, re.IGNORECASE)]
    return min(hits, key=len) if hits else None


def check_item(raw: dict[str, Any], candidates: dict[str, tuple[date, str]], doc_text: str) -> Obligation | None:
    """One model item as an Obligation: its date and quote come from the document, its notice period is checked."""
    picked = candidates.get(str(raw.get("date_id")))
    if picked is None:
        return None
    due, line = picked
    notice = max(0, int(raw.get("notice_days") or 0))
    rule_quote = (raw.get("rule_quote") or "").strip() or None
    notes = []
    if rule_quote and not quote_in_text(rule_quote, doc_text):
        rule_quote = None
    if notice and not (rule_quote and number_in_text(notice, rule_quote)):
        rule_quote = find_rule_quote(notice, doc_text)
        if rule_quote is None:
            notes.append(f"a notice period of {notice} days is not written in the document")
    kind = raw.get("kind") if raw.get("kind") in KINDS else "other"
    return Obligation(id=new_id("obl"), kind=kind, title=str(raw.get("title") or "Deadline")[:120], due_date=due,
                      notice_days=notice, amount=raw.get("amount"), currency=raw.get("currency"),
                      auto_renews=raw.get("auto_renews"), action=str(raw.get("action") or "")[:300],
                      date_quote=line.strip(" |"), rule_quote=rule_quote,
                      status="done" if raw.get("status") == "done" else "upcoming",
                      verified=not notes, check_note="; ".join(notes) or None)


def read_document(store: Store, llm: TokenFactory, doc: Document, today: date, cfg: dict[str, Any]) -> Document:
    """Read one document with Nemotron and store its obligations. Raises the gateway's errors."""
    rcfg = cfg["reader"]
    dates = find_dates(doc.redacted)
    if not dates:
        store.set_obligations(doc.id, [])
        doc.read_status, doc.read_by, doc.read_note = "read", None, "no dates are written in this document"
        store.update_document(doc)
        return doc
    candidates = {f"D{i + 1}": d for i, d in enumerate(dates)}
    listing = "\n".join(f"{k}: {d.isoformat()} ({d.strftime('%a %d %b %Y')}) — line: {line}"
                        for k, (d, line) in candidates.items())
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Today is {today.isoformat()}.\nDocument: {doc.title}\n\n{doc.redacted}"
                                            f"\n\nDates written in the document:\n{listing}"}]
    result = ask_model(llm, store, "reader", messages, purpose=f"read “{doc.title}”",
                       response_format={"type": "json_schema", "json_schema": read_schema(list(candidates))},
                       thinking=rcfg["thinking"], max_tokens=int(rcfg["max_tokens"]))
    data = _parse_json(result.content)
    items: list[Obligation] = []
    seen: set[tuple[str, int]] = set()
    for raw in data.get("obligations", []):
        key = (str(raw.get("date_id")), int(raw.get("notice_days") or 0))
        item = check_item(raw, candidates, doc.redacted)
        if item is not None and key not in seen:
            seen.add(key)
            items.append(item)
    counterparty = data.get("counterparty")
    for o in items:
        o.doc_id = doc.id
        o.counterparty = counterparty
    store.set_obligations(doc.id, items)
    doc.summary = str(data.get("summary") or "")[:300] or None
    doc.read_status, doc.read_by = "read", result.call.model
    unverified = sum(not o.verified for o in items)
    doc.read_note = f"{len(items)} of {len(dates)} dates matter" + (f", {unverified} to check by hand" if unverified
                                                                    else "")
    store.update_document(doc)
    return doc
