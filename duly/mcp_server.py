"""Duly as an MCP server, so other agents (for example Hermes Agent) can use your paperwork memory.

    python -m duly.mcp_server            # stdio; uses DULY_DATA_DIR or data/user

Every tool returns redacted text: placeholders instead of personal details, exactly what Duly's own assistant sees.
So the agent's model (for Hermes, Nemotron through its Nebius Token Factory provider) never receives the values
in your documents. What you type to that agent yourself does not pass through Duly's firewall.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from duly.brief import morning_check
from duly.config import default_data_dir, load_config
from duly.gateway import TavilySearch
from duly.llm import SpendLedger, TokenFactory
from duly.reader import read_document
from duly.skills import load_skills
from duly.store import Store
from duly.tools import Context, run_tool

CFG = load_config()
LEDGER = SpendLedger.from_config(CFG)
LLM = TokenFactory(CFG, ledger=LEDGER)
WEB = TavilySearch(CFG, ledger=LEDGER)

mcp = MCPServer("duly", instructions="Duly keeps the user's paperwork: documents, dated obligations (renewals, payments, "
              "notice periods, warranty ends), remembered facts and drafts. Personal details appear as placeholders "
              "like [PERSON_1]; keep them as written. Never compute dates: use the dates these tools return.")


def _store() -> Store:
    # A fresh Store for every call: the app writes to the same folder, and a vault kept from an earlier call would
    # mask with old values and could be saved over the app's new entries.
    return Store(default_data_dir())


def _ctx() -> Context:
    # What another agent asks Duly to store may hold details its user typed there: redact it in full.
    return Context(store=_store(), today=date.today(), skills=load_skills(), web=WEB, writes_are_user_text=True)


def _call(name: str, args: dict) -> str:
    """A tool result, masked against the vault like every tool result Duly's own assistant receives."""
    ctx = _ctx()
    result = run_tool(ctx, name, args)
    # ctx.store.vault is read after the tool ran: a tool that writes reloads the vault and may add entries.
    return ctx.store.vault.mask_known(json.dumps(result, ensure_ascii=False, indent=1))


@mcp.tool()
def list_deadlines(within_days: int = 60) -> str:
    """Dated obligations from the user's documents and reminders, ordered by the last day to act."""
    return _call("list_deadlines", {"within_days": within_days})


@mcp.tool()
def search_documents(query: str) -> str:
    """Search the user's documents (redacted) and remembered facts."""
    return _call("search_documents", {"query": query})


@mcp.tool()
def save_draft(kind: str, title: str, body: str, to: str = "") -> str:
    """Save an e-mail, letter or checklist for the user to review. Keep placeholders as written."""
    return _call("save_draft", {"kind": kind, "title": title, "body": body, "to": to or None})


@mcp.tool()
def add_reminder(title: str, date_iso: str, note: str = "") -> str:
    """Add a dated reminder (date as YYYY-MM-DD)."""
    return _call("add_reminder", {"title": title, "date": date_iso, "note": note})


@mcp.tool()
def remember(fact: str) -> str:
    """Remember a lasting fact or preference about the user."""
    return _call("remember", {"fact": fact})


@mcp.tool()
def morning_brief() -> str:
    """Run today's morning check: what needs attention, most urgent first (redacted)."""
    store = _store()
    brief = morning_check(store, LLM, date.today(), CFG)
    return store.vault.mask_known(brief.text)


@mcp.tool()
def add_document(path: str, title: str = "") -> str:
    """Add a text or Markdown file from this computer: personal details are hidden, then Nemotron reads its dates."""
    store = _store()
    p = Path(path).expanduser()
    text = p.read_text(encoding="utf-8", errors="replace")
    doc = store.add_document(title, text, filename=p.name, max_chars=int(CFG["reader"]["max_document_chars"]))
    if LLM.available:
        doc = read_document(store, LLM, doc, date.today(), CFG)
    return store.vault.mask_known(json.dumps({"document": doc.title, "hidden": doc.hidden, "read": doc.read_note},
                                             ensure_ascii=False))


if __name__ == "__main__":
    mcp.run()
