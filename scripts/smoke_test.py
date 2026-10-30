"""Checks the live services: the configured Nemotron models exist on Token Factory, each answers once,
and Tavily answers one search. Costs well under one cent and one Tavily credit.

    python scripts/smoke_test.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from duly.config import load_config  # noqa: E402
from duly.gateway import TavilySearch  # noqa: E402
from duly.llm import TokenFactory  # noqa: E402
from duly.store import Store  # noqa: E402


def main() -> int:
    cfg = load_config()
    llm = TokenFactory(cfg)
    if not llm.available:
        print("NEBIUS_API_KEY is not set (.env)")
        return 1
    listed = {m.id for m in llm._client.models.list()}
    ok = True
    for role, model in cfg["llm"]["models"].items():
        found = model in listed
        ok &= found
        print(f"{role:7s} {model:45s} {'listed' if found else 'NOT LISTED'}")
        if not found:
            close = sorted(m for m in listed if "nemotron" in m.lower())
            print("        Nemotron models listed:", ", ".join(close))
    for role in ("agent", "brief"):
        r = llm.chat(role, [{"role": "user", "content": "Reply with the single word: ready"}], label="smoke",
                     thinking="off", max_tokens=20)
        print(f"{role:7s} answered {r.content!r} in {r.call.latency_s:.1f} s, "
              f"{r.call.prompt_tokens}+{r.call.completion_tokens} tokens, ${r.call.cost_usd:.6f}")
    web = TavilySearch(cfg, cache_dir=Path(tempfile.mkdtemp()))
    if web.available:
        t0 = time.perf_counter()
        sources = web.search(Store(Path(tempfile.mkdtemp())), "Dell laptop hardware warranty claim support")
        print(f"tavily  {len(sources)} results in {time.perf_counter() - t0:.1f} s; first: {sources[0].url if sources else '-'}")
    else:
        print("TAVILY_API_KEY is not set (.env)")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
