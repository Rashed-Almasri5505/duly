"""Nebius Token Factory client: model roles, reasoning handling, cost accounting and spend caps.

Token Factory is OpenAI-compatible, so this wraps the OpenAI SDK. Reasoning is switched with
chat_template_kwargs: enable_thinking false turns it off, low_effort true makes Super reason briefly.
Every call is metered from `usage` and booked in a small JSON ledger that enforces the daily and total
spend caps of the public demo. Privacy is not handled here: callers pass only redacted text, and the
assistant checks every payload with the vault before it calls `chat`.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from openai import OpenAI

from duly.config import REPO_ROOT, get_secret

Thinking = Literal["default", "off", "low"]
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMError(RuntimeError):
    """A Token Factory call failed after the SDK's own retries."""


class BudgetExceeded(RuntimeError):
    """A spend cap would be exceeded; the app switches to recorded examples."""


@dataclass
class LLMCall:
    """Metering record of one model call."""

    label: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    cost_usd: float = 0.0
    finish_reason: str | None = None
    ok: bool = True
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class ChatResult:
    content: str
    reasoning: str | None
    tool_calls: list[ToolCall]
    call: LLMCall
    assistant_message: dict[str, Any] = field(default_factory=dict)


def tier_of(model_id: str) -> str | None:
    low = model_id.lower()
    return next((t for t in ("ultra", "super", "lightning", "nano") if t in low), None)


def badge_of(model_id: str) -> str:
    """Short model name for the UI (Nano / Super)."""
    return f"Nemotron {(tier_of(model_id) or model_id).title()}"


class SpendLedger:
    """Daily and total spend in USD and in Tavily credits, in a JSON file shared by all sessions.

    Writes are atomic (temporary file + rename) under a lock. An unreadable ledger fails closed.
    """

    _locks: dict[Path, threading.Lock] = {}
    _guard = threading.Lock()

    def __init__(self, path: Path, daily_usd: float, total_usd: float, *,
                 daily_credits: float | None = None, total_credits: float | None = None) -> None:
        self.path = path
        self.daily_usd, self.total_usd = daily_usd, total_usd
        self.daily_credits, self.total_credits = daily_credits, total_credits
        with SpendLedger._guard:
            self._lock = SpendLedger._locks.setdefault(path.resolve(), threading.Lock())

    @classmethod
    def from_config(cls, cfg: dict[str, Any], today: date | None = None) -> SpendLedger:
        return cls(REPO_ROOT / cfg["budget"]["ledger_path"],
                   demo_limit(cfg, "daily_usd", "DEMO_DAILY_BUDGET_USD", today=today),
                   demo_limit(cfg, "total_usd", "DEMO_TOTAL_BUDGET_USD", today=today),
                   daily_credits=demo_limit(cfg, "tavily_daily_credits", "DEMO_TAVILY_DAILY_CREDITS", today=today),
                   total_credits=demo_limit(cfg, "tavily_total_credits", "DEMO_TAVILY_TOTAL_CREDITS", today=today))

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as exc:
            raise BudgetExceeded(f"the spend ledger is unreadable ({exc}); live calls are paused")
        if not isinstance(data, dict):
            raise BudgetExceeded("the spend ledger is malformed; live calls are paused")
        return data

    def _book(self, total_key: str, days_key: str, amount: float) -> None:
        with self._lock:
            data = self._read()
            today = date.today().isoformat()
            data[total_key] = float(data.get(total_key, 0.0)) + amount
            days = data.setdefault(days_key, {})
            days[today] = float(days.get(today, 0.0)) + amount
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            for attempt in range(40):     # a reader in another process can briefly hold the file on Windows
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:
                    if attempt == 39:
                        tmp.unlink(missing_ok=True)
                        raise BudgetExceeded("could not record spend; live calls are paused") from None
                    time.sleep(0.025)

    def add(self, cost_usd: float) -> None:
        self._book("total_usd", "days", cost_usd)

    def add_credits(self, credits: float) -> None:
        self._book("total_credits", "credit_days", credits)

    def _used(self, total_key: str, days_key: str) -> tuple[float, float]:
        with self._lock:
            data = self._read()
        days = data.get(days_key, {}) if isinstance(data.get(days_key, {}), dict) else {}
        return float(days.get(date.today().isoformat(), 0.0)), float(data.get(total_key, 0.0))

    def spent(self) -> tuple[float, float]:
        return self._used("total_usd", "days")

    def credits(self) -> tuple[float, float]:
        return self._used("total_credits", "credit_days")

    def check(self, needed_usd: float = 0.0) -> None:
        today, total = self.spent()
        if today + needed_usd > self.daily_usd:
            raise BudgetExceeded(f"the demo's daily budget of ${self.daily_usd:.2f} is used up")
        if total + needed_usd > self.total_usd:
            raise BudgetExceeded(f"the demo's total budget of ${self.total_usd:.2f} is used up")

    def check_credits(self, needed: float = 1.0) -> None:
        today, total = self.credits()
        if self.daily_credits is not None and today + needed > self.daily_credits:
            raise BudgetExceeded(f"the demo's daily budget of {self.daily_credits:g} web searches is used up")
        if self.total_credits is not None and total + needed > self.total_credits:
            raise BudgetExceeded(f"the demo's total budget of {self.total_credits:g} web searches is used up")

    def remaining_usd(self) -> float:
        today, total = self.spent()
        return max(0.0, min(self.daily_usd - today, self.total_usd - total))


def in_judging_period(cfg: dict[str, Any], today: date | None = None) -> bool:
    start, end = (date.fromisoformat(d) for d in cfg["budget"]["judging_period_utc"])
    return start <= (today or datetime.now(timezone.utc).date()) <= end


def demo_limit(cfg: dict[str, Any], key: str, secret: str, *, today: date | None = None) -> float:
    """A demo limit: the secret if it is a valid number, else config.yaml's value; never below the judging
    value during the judging period, so the judges are not cut off."""
    budget = cfg["budget"]
    value = float(budget[key])
    raw = get_secret(secret)
    try:
        from_secret = float(raw) if raw is not None else None
    except ValueError:
        from_secret = None
    if from_secret is not None and math.isfinite(from_secret) and from_secret >= 0:
        value = from_secret
    return max(value, float(budget["judging"][key])) if in_judging_period(cfg, today) else value


class TokenFactory:
    """Role-based access to the Nemotron models on Nebius Token Factory."""

    def __init__(self, cfg: dict[str, Any], *, ledger: SpendLedger | None = None, client: Any | None = None) -> None:
        llm = cfg["llm"]
        self.models: dict[str, str] = dict(llm["models"])
        self.prices: dict[str, list[float]] = llm["prices_usd_per_1m_tokens"]
        self.sampling: dict[str, list[float]] = llm["sampling"]
        self.base_url: str = get_secret("NEBIUS_BASE_URL") or llm["base_url"]
        self.ledger = ledger
        self.calls: list[LLMCall] = []
        self._lock = threading.Lock()
        api_key = get_secret("NEBIUS_API_KEY")
        if client is not None:
            self._client = client
        elif api_key:
            self._client = OpenAI(base_url=self.base_url, api_key=api_key, timeout=float(llm["timeout_s"]),
                                  max_retries=int(llm["max_retries"]))
        else:
            self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def model_for(self, role: str) -> str:
        return self.models.get(role, role)

    def cost_usd(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        price_in, price_out = self.prices.get(tier_of(model) or "", [0.0, 0.0])
        return (prompt_tokens * price_in + completion_tokens * price_out) / 1e6

    def _book(self, call: LLMCall) -> None:
        with self._lock:
            self.calls.append(call)
        if self.ledger is not None and call.cost_usd:
            self.ledger.add(call.cost_usd)

    def chat(self, role: str, messages: list[dict[str, Any]], *, label: str,
             tools: list[dict[str, Any]] | None = None, response_format: dict[str, Any] | None = None,
             thinking: Thinking = "default", max_tokens: int = 2048) -> ChatResult:
        """One chat completion. Raises LLMError or BudgetExceeded."""
        if self._client is None:
            raise LLMError("NEBIUS_API_KEY is not set")
        if self.ledger is not None:
            self.ledger.check()
        model = self.model_for(role)
        temperature, top_p = self.sampling.get(tier_of(model) or "", [0.6, 0.95])
        kwargs: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens,
                                  "temperature": temperature, "top_p": top_p}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if response_format:
            kwargs["response_format"] = response_format
        if thinking == "off":
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        elif thinking == "low":
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": True, "low_effort": True}}
        t0 = time.perf_counter()
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # the SDK has already retried 429, 5xx and timeouts
            call = LLMCall(label, model, latency_s=round(time.perf_counter() - t0, 3), ok=False,
                           error=f"{type(exc).__name__}: {str(exc)[:200]}")
            self._book(call)
            raise LLMError(call.error) from exc
        usage = resp.usage
        call = LLMCall(label, model, prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                       completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                       latency_s=round(time.perf_counter() - t0, 3), finish_reason=resp.choices[0].finish_reason)
        call.cost_usd = self.cost_usd(model, call.prompt_tokens, call.completion_tokens)
        self._book(call)
        msg = resp.choices[0].message
        content, reasoning = split_reasoning(msg)
        tool_calls = [ToolCall(tc.id, tc.function.name, tc.function.arguments or "{}") for tc in (msg.tool_calls or [])]
        assistant: dict[str, Any] = {"role": "assistant", "content": content or None}
        if tool_calls:
            assistant["tool_calls"] = [{"id": t.id, "type": "function",
                                        "function": {"name": t.name, "arguments": t.arguments}} for t in tool_calls]
        return ChatResult(content, reasoning, tool_calls, call, assistant)


def split_reasoning(message: Any) -> tuple[str, str | None]:
    """Separate the answer from the reasoning (Token Factory uses reasoning_content or reasoning)."""
    content = message.content or ""
    extra = getattr(message, "model_extra", None) or {}
    reasoning = extra.get("reasoning_content") or extra.get("reasoning")
    think = THINK_RE.findall(content)
    if think:
        reasoning = (reasoning or "") + "\n".join(t[7:-8] for t in think)
        content = THINK_RE.sub("", content)
    return content.strip(), reasoning or None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
