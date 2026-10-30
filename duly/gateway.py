"""The only way out: every model call and web search passes through here.

Before anything leaves this computer, the payload is checked against the vault: if it contains any personal
value the vault knows (a name, an ID number, an IBAN ...), the call is blocked. Each call that goes out is
written to the outbound log with the exact text sent, so the user can see what each service received.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from duly.config import REPO_ROOT, get_secret
from duly.llm import BudgetExceeded, ChatResult, LLMError, SpendLedger, TokenFactory, utc_now
from duly.privacy import PrivacyError
from duly.store import Store


def _payload_text(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> str:
    """The messages as readable text: what the model receives (tool definitions are code, not user data)."""
    lines = []
    for m in messages:
        role = m.get("role", "?")
        if m.get("content"):
            lines.append(f"[{role}] {m['content']}")
        for tc in m.get("tool_calls") or []:
            lines.append(f"[{role} → tool] {tc['function']['name']}({tc['function']['arguments']})")
    if tools:
        lines.append(f"[tools] {', '.join(t['function']['name'] for t in tools)}")
    return "\n\n".join(lines)


def ask_model(llm: TokenFactory, store: Store, role: str, messages: list[dict[str, Any]], *, purpose: str,
              tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> ChatResult:
    """A Token Factory call with the privacy check and the outbound log. Raises PrivacyError, LLMError or
    BudgetExceeded."""
    model = llm.model_for(role)
    destination = f"Nebius Token Factory · {model}"
    text = _payload_text(messages, tools)
    try:
        store.vault.check_outbound(json.dumps(messages, ensure_ascii=False), destination)
    except PrivacyError:
        store.log_outbound(destination, purpose, store.vault.redact(text)[0], check="blocked")
        raise
    store.log_outbound(destination, purpose, text)
    return llm.chat(role, messages, label=purpose, tools=tools, **kwargs)


class Source(BaseModel):
    title: str
    url: str
    snippet: str
    via: str            # "live" or "cached"
    fetched_at: str

    @property
    def label(self) -> str:
        return "via Tavily (live)" if self.via == "live" else f"via Tavily (cached {self.fetched_at[:10]})"


class TavilySearch:
    """Tavily web search with a shared cache. Queries are checked by the vault like any other outbound call."""

    def __init__(self, cfg: dict[str, Any], *, ledger: SpendLedger | None = None, client: Any | None = None,
                 cache_dir: Path | None = None, cache_hours: float = 24.0) -> None:
        tav = cfg["tavily"]
        self.depth: str = tav["search_depth"]
        self.max_results: int = int(tav["max_results"])
        self.cache_dir = cache_dir or REPO_ROOT / tav["cache_dir"]
        self.cache_hours = cache_hours
        self.ledger = ledger
        self.live_calls = 0
        if client is not None:
            self._client = client
        else:
            key = get_secret("TAVILY_API_KEY")
            if key:
                from tavily import TavilyClient
                self._client = TavilyClient(api_key=key)
            else:
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def _cache_path(self, query: str) -> Path:
        return self.cache_dir / (hashlib.sha256(f"{self.depth}|{query.lower().strip()}".encode()).hexdigest()[:24] + ".json")

    def search(self, store: Store, query: str, *, purpose: str = "web search") -> list[Source]:
        destination = "Tavily search"
        try:
            store.vault.check_outbound(query, destination)
        except PrivacyError:
            store.log_outbound(destination, purpose, store.vault.redact(query)[0], check="blocked")
            raise
        path = self._cache_path(query)
        if path.exists():
            cached = json.loads(path.read_text(encoding="utf-8"))
            age = datetime.now(timezone.utc) - datetime.fromisoformat(cached["fetched_at"])
            if age < timedelta(hours=self.cache_hours) or self._client is None:
                return [Source(**r, via="cached", fetched_at=cached["fetched_at"]) for r in cached["results"]]
        if self._client is None:
            raise LLMError("TAVILY_API_KEY is not set")
        if self.ledger is not None:
            self.ledger.check_credits(1)
        store.log_outbound(destination, purpose, query)
        t0 = time.perf_counter()
        try:
            resp = self._client.search(query, search_depth=self.depth, max_results=self.max_results,
                                       include_answer=False, include_raw_content=False, topic="general")
        except Exception as exc:
            raise LLMError(f"Tavily search failed: {type(exc).__name__}: {str(exc)[:160]}") from exc
        self.live_calls += 1
        if self.ledger is not None:
            self.ledger.add_credits(1)
        results = [{"title": (r.get("title") or r.get("url", ""))[:160], "url": r.get("url", ""),
                    "snippet": " ".join((r.get("content") or "").split())[:400]}
                   for r in resp.get("results", []) if r.get("url")]
        fetched_at = utc_now()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"query": query, "fetched_at": fetched_at, "results": results,
                                    "latency_s": round(time.perf_counter() - t0, 2)}, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        return [Source(**r, via="live", fetched_at=fetched_at) for r in results]


__all__ = ["ask_model", "TavilySearch", "Source", "PrivacyError", "LLMError", "BudgetExceeded"]
