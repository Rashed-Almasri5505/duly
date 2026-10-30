"""Duly's memory: documents, dated obligations, remembered facts, drafts and the outbound log.

Everything lives as JSON files in one folder you own (data/user by default, or DULY_DATA_DIR). Each document
is kept twice: the original, which never leaves this computer, and the redacted copy, the only one a model
may read. Remembered facts are redacted before they are stored. Deleting an item removes it from disk.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from duly.llm import utc_now
from duly.privacy import PLACEHOLDER_RE, Vault, count_by_kind

ObligationKind = Literal["renewal", "expiry", "payment", "cancel_or_notice", "warranty_end", "return_window",
                         "other"]
KIND_LABELS = {"renewal": "Renewal", "expiry": "Expiry", "payment": "Payment due",
               "cancel_or_notice": "Notice or cancel by", "warranty_end": "Warranty ends",
               "return_window": "Return window ends", "other": "Deadline"}


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


class Document(BaseModel):
    id: str
    title: str
    kind: str = "other"
    filename: str | None = None
    added_at: str = Field(default_factory=utc_now)
    original: str                      # stays on this computer
    redacted: str                      # the only text a model may read
    hidden: dict[str, int] = {}        # personal details hidden, by kind
    truncated: bool = False
    summary: str | None = None
    read_status: Literal["unread", "read", "recorded", "failed"] = "unread"
    read_by: str | None = None
    read_note: str | None = None


class Obligation(BaseModel):
    id: str
    doc_id: str | None = None          # None for a reminder the user or the assistant added
    kind: ObligationKind = "other"
    title: str
    counterparty: str | None = None
    due_date: date
    notice_days: int = 0
    amount: float | None = None
    currency: str | None = None
    auto_renews: bool | None = None
    action: str = ""
    date_quote: str | None = None
    rule_quote: str | None = None
    status: Literal["upcoming", "done", "unknown"] = "upcoming"
    verified: bool = False
    check_note: str | None = None
    source: Literal["document", "reminder"] = "document"

    @property
    def act_by(self) -> date:
        """The last day to act: the due date minus the notice period the document asks for."""
        return self.due_date - timedelta(days=self.notice_days)


class Note(BaseModel):
    id: str
    text: str                          # redacted
    created_at: str = Field(default_factory=utc_now)
    source: Literal["assistant", "you"] = "assistant"


class Draft(BaseModel):
    id: str
    kind: str = "email"
    title: str
    to: str | None = None
    body: str                          # redacted; restored only on screen and in downloads
    created_at: str = Field(default_factory=utc_now)
    doc_id: str | None = None


class Outbound(BaseModel):
    """One call that left this computer, with the exact text that was sent."""

    at: str = Field(default_factory=utc_now)
    destination: str
    purpose: str
    chars: int
    placeholders: int
    check: Literal["passed", "blocked"] = "passed"
    payload: str


class Profile(BaseModel):
    name: str | None = None
    lead_days: int = 7
    people: list[str] = []             # names to always hide (yours, family members)


class Store:
    """All of one user's data, in one folder."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.vault = Vault(root / "vault.json")

    # ----- files -----
    def _path(self, name: str) -> Path:
        return self.root / name

    def _load(self, name: str, default: Any) -> Any:
        path = self._path(name)
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, name: str, data: Any) -> None:
        path = self._path(name)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(path)

    # ----- profile -----
    def profile(self) -> Profile:
        return Profile(**self._load("profile.json", {}))

    def save_profile(self, profile: Profile) -> None:
        with self._lock:
            for person in ([profile.name] if profile.name else []) + profile.people:
                self.vault.add_known_person(person)
            self.vault.save()
            self._save("profile.json", profile.model_dump())

    # ----- documents -----
    def documents(self) -> list[Document]:
        return [Document(**d) for d in self._load("documents.json", [])]

    def document(self, doc_id: str) -> Document | None:
        return next((d for d in self.documents() if d.id == doc_id), None)

    def add_document(self, title: str, text: str, *, kind: str = "other", filename: str | None = None,
                     max_chars: int = 20000) -> Document:
        with self._lock:
            truncated = len(text) > max_chars
            original = text[:max_chars]
            redacted, used = self.vault.redact(original, source=title)
            self.vault.save()
            doc = Document(id=new_id("doc"), title=title, kind=kind, filename=filename, original=original,
                           redacted=redacted, hidden=count_by_kind(used), truncated=truncated)
            docs = self.documents() + [doc]
            self._save("documents.json", [d.model_dump() for d in docs])
            return doc

    def update_document(self, doc: Document) -> None:
        with self._lock:
            docs = [doc if d.id == doc.id else d for d in self.documents()]
            self._save("documents.json", [d.model_dump() for d in docs])

    def delete_document(self, doc_id: str) -> None:
        with self._lock:
            self._save("documents.json", [d.model_dump() for d in self.documents() if d.id != doc_id])
            self._save("obligations.json", [o.model_dump() for o in self.obligations() if o.doc_id != doc_id])

    # ----- obligations and reminders -----
    def obligations(self) -> list[Obligation]:
        return [Obligation(**o) for o in self._load("obligations.json", [])]

    def set_obligations(self, doc_id: str, items: list[Obligation]) -> None:
        with self._lock:
            kept = [o for o in self.obligations() if o.doc_id != doc_id]
            self._save("obligations.json", [o.model_dump() for o in kept + items])

    def _safe(self, text: str, from_model: bool) -> str:
        """Text written by the model only needs known values masked (it never saw real ones, and scanning it for
        new details would turn phrases like 'Dear Dell Support' into vault entries). Text the user typed is
        scanned in full."""
        if from_model:
            return self.vault.mask_known(text)
        safe, _ = self.vault.redact(text)
        self.vault.save()
        return safe

    def add_reminder(self, title: str, due: date, note: str = "", *, from_model: bool = True) -> Obligation:
        with self._lock:
            safe_title, safe_note = self._safe(title, from_model), self._safe(note, from_model)
            item = Obligation(id=new_id("rem"), title=safe_title, due_date=due, action=safe_note,
                              source="reminder", verified=True, kind="other")
            self._save("obligations.json", [o.model_dump() for o in self.obligations() + [item]])
            return item

    def delete_obligation(self, item_id: str) -> None:
        with self._lock:
            self._save("obligations.json", [o.model_dump() for o in self.obligations() if o.id != item_id])

    def upcoming(self, today: date, within_days: int) -> list[Obligation]:
        """Obligations still open whose last day to act falls before today + within_days (overdue ones too,
        until their due date has passed)."""
        horizon = today + timedelta(days=within_days)
        items = [o for o in self.obligations()
                 if o.status != "done" and o.due_date >= today and o.act_by <= horizon]
        return sorted(items, key=lambda o: o.act_by)

    # ----- remembered facts -----
    def notes(self) -> list[Note]:
        return [Note(**n) for n in self._load("notes.json", [])]

    def add_note(self, text: str, source: Literal["assistant", "you"] = "assistant") -> Note:
        with self._lock:
            note = Note(id=new_id("note"), text=self._safe(text, from_model=source == "assistant"), source=source)
            self._save("notes.json", [n.model_dump() for n in self.notes() + [note]])
            return note

    def forget_note(self, note_id: str) -> bool:
        with self._lock:
            notes = self.notes()
            kept = [n for n in notes if n.id != note_id]
            self._save("notes.json", [n.model_dump() for n in kept])
            return len(kept) < len(notes)

    # ----- drafts -----
    def drafts(self) -> list[Draft]:
        return [Draft(**d) for d in self._load("drafts.json", [])]

    def add_draft(self, title: str, body: str, *, kind: str = "email", to: str | None = None,
                  doc_id: str | None = None, from_model: bool = True) -> Draft:
        with self._lock:
            draft = Draft(id=new_id("draft"), kind=kind, title=self._safe(title, from_model),
                          to=self._safe(to, from_model) if to else None, body=self._safe(body, from_model),
                          doc_id=doc_id)
            self._save("drafts.json", [d.model_dump() for d in self.drafts() + [draft]])
            return draft

    def delete_draft(self, draft_id: str) -> None:
        with self._lock:
            self._save("drafts.json", [d.model_dump() for d in self.drafts() if d.id != draft_id])

    # ----- morning briefs -----
    def briefs(self) -> list[dict[str, Any]]:
        return self._load("briefs.json", [])

    def save_brief(self, brief: dict[str, Any]) -> None:
        with self._lock:
            self._save("briefs.json", (self.briefs() + [brief])[-30:])

    # ----- outbound log -----
    def log_outbound(self, destination: str, purpose: str, payload: str, check: str = "passed") -> None:
        record = Outbound(destination=destination, purpose=purpose, chars=len(payload),
                          placeholders=len(PLACEHOLDER_RE.findall(payload)), check=check, payload=payload[:30000])
        with self._lock:
            with self._path("outbound.jsonl").open("a", encoding="utf-8") as f:
                f.write(record.model_dump_json() + "\n")

    def outbound(self) -> list[Outbound]:
        path = self._path("outbound.jsonl")
        if not path.exists():
            return []
        return [Outbound(**json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line]

    # ----- search over the redacted memory -----
    def search(self, query: str, k: int = 3, max_chars: int = 3500) -> list[dict[str, str]]:
        """The redacted documents and remembered facts that share the most words with the query.

        Personal documents are short, so a match returns the whole document (up to max_chars): a paragraph alone
        missed details such as the service tag two paragraphs away (seen on 2026-10-30).
        """
        words = _terms(query)
        if not words:
            return []
        candidates: list[tuple[float, dict[str, str]]] = []
        for doc in self.documents():
            terms = _terms(doc.redacted)
            score = len(words & terms) + 2.0 * len(words & _terms(doc.title)) + 0.5 * len(words & _terms(doc.kind))
            if score > 0:
                text = doc.redacted if len(doc.redacted) <= max_chars else doc.redacted[:max_chars] + "\n[...]"
                candidates.append((score, {"source": doc.title, "doc_id": doc.id, "text": text}))
        for note in self.notes():
            score = len(words & _terms(note.text))
            if score > 0:
                candidates.append((score + 0.5, {"source": "remembered fact", "doc_id": note.id, "text": note.text}))
        candidates.sort(key=lambda c: c[0], reverse=True)
        return [c[1] for c in candidates[:k]]

    # ----- everything -----
    def wipe(self) -> None:
        """Delete every file of this user (documents, memory, vault, drafts and the log)."""
        with self._lock:
            shutil.rmtree(self.root, ignore_errors=True)
            self.root.mkdir(parents=True, exist_ok=True)
            self.vault = Vault(self.root / "vault.json")


_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "my", "i", "me", "it", "be",
         "do", "does", "did", "can", "what", "when", "how", "with", "this", "that", "at", "by", "from", "am", "still",
         "any", "have", "has", "will", "would", "should", "if", "not", "no", "yes", "you", "your", "we", "our"}


def _terms(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w[:-1] if len(w) > 4 and w.endswith("s") else w for w in words if w not in _STOP and len(w) > 1}
