"""Shared test helpers: a scripted stand-in for the Token Factory client, and sample stores."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from duly.config import load_config  # noqa: E402
from duly.llm import TokenFactory  # noqa: E402
from duly.samples import install_samples  # noqa: E402
from duly.store import Store  # noqa: E402

TODAY = date(2026, 10, 30)


def message(content: str | None = None, tool_calls: list[tuple[str, dict[str, Any]]] | None = None) -> SimpleNamespace:
    calls = [SimpleNamespace(id=f"call-{i}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
             for i, (name, args) in enumerate(tool_calls or [])]
    return SimpleNamespace(content=content, tool_calls=calls or None, model_extra={})


@dataclass
class FakeClient:
    """Answers chat calls from a script: a function of (model, messages, kwargs) -> message."""

    script: Any
    requests: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append(kwargs)
        msg = self.script(kwargs["model"], kwargs["messages"], kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                               usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                                                     completion_tokens_details=None))


@pytest.fixture
def cfg() -> dict[str, Any]:
    return load_config()


@pytest.fixture
def sample_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "user")
    install_samples(store, TODAY)
    return store


def fake_llm(cfg: dict[str, Any], script: Any) -> tuple[TokenFactory, FakeClient]:
    client = FakeClient(script)
    return TokenFactory(cfg, client=client), client
