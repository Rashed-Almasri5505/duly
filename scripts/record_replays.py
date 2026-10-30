"""Records live conversations over the fictional samples, for the app's offline mode (data/replays/*.json).

Each replay is a real run: the same Turn the app would show, with its steps, costs, sources and drafts. The app
labels them "Recorded live on <date>". Costs about one cent per question.

    python scripts/record_replays.py
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from duly.agent import run_turn  # noqa: E402
from duly.config import REPO_ROOT, load_config  # noqa: E402
from duly.gateway import TavilySearch  # noqa: E402
from duly.llm import TokenFactory  # noqa: E402
from duly.samples import install_samples  # noqa: E402
from duly.skills import load_skills  # noqa: E402
from duly.store import Store  # noqa: E402
from duly.tools import Context  # noqa: E402

QUESTIONS = [
    "My laptop screen started flickering. Am I still covered?",
    "I don't want to renew the gym. What do I do, and by when?",
    "What do I need to do in the next 30 days?",
]


def main() -> int:
    cfg = load_config()
    out_dir = REPO_ROOT / "data" / "replays"
    out_dir.mkdir(parents=True, exist_ok=True)
    llm = TokenFactory(cfg)
    for i, q in enumerate(QUESTIONS, 1):
        store = Store(Path(tempfile.mkdtemp()))
        install_samples(store, date.today())
        ctx = Context(store=store, today=date.today(), skills=load_skills(), web=TavilySearch(cfg))
        turn = run_turn(ctx, llm, q, cfg)
        drafts = [d.model_dump(mode="json") for d in store.drafts() if d.id in turn.drafts]
        slug = re.sub(r"[^a-z0-9]+", "-", q.lower()).strip("-")[:40]
        path = out_dir / f"{i:02d}-{slug}.json"
        path.write_text(json.dumps({"turn": turn.model_dump(mode="json"), "drafts": drafts}, indent=1,
                                   ensure_ascii=False), encoding="utf-8")
        print(f"{path.name}: {len(turn.steps)} steps, {turn.seconds} s, ${turn.cost_usd:.4f}, "
              f"skills {turn.skills_used}, error {turn.error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
