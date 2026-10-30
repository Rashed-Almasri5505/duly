"""Ask Duly a question from the command line, over the fictional sample documents.

    python scripts/ask.py "My laptop screen flickers. Am I still covered?"
    python scripts/ask.py --data data/user "What is due this month?"    # your own data folder

Prints each step (model, tokens, cost, tool), the answer as the model wrote it (with placeholders), the answer
as you see it (real values restored), the sources and the drafts.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from duly.agent import Step, run_turn  # noqa: E402
from duly.config import load_config  # noqa: E402
from duly.gateway import TavilySearch  # noqa: E402
from duly.llm import TokenFactory  # noqa: E402
from duly.recorded import load_recording, recorded_obligations  # noqa: E402
from duly.samples import SAMPLE_PERSON, load_samples  # noqa: E402
from duly.skills import load_skills  # noqa: E402
from duly.store import Profile, Store  # noqa: E402
from duly.tools import Context  # noqa: E402


def sample_store(today: date) -> Store:
    store = Store(Path(tempfile.mkdtemp()))
    store.save_profile(Profile(name=SAMPLE_PERSON))
    recording = load_recording()
    for s in load_samples(today):
        doc = store.add_document(s.title, s.text, kind=s.kind)
        summary, items = recorded_obligations(s.slug, doc.id, today, recording) if recording else (None, [])
        store.set_obligations(doc.id, items)
        doc.summary, doc.read_status = summary, "recorded"
        store.update_document(doc)
    return store


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--data", help="your data folder (default: a temporary copy of the samples)")
    ap.add_argument("--no-web", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    today = date.today()
    store = Store(Path(args.data)) if args.data else sample_store(today)
    llm = TokenFactory(cfg)
    ctx = Context(store=store, today=today, skills=load_skills(), web=TavilySearch(cfg), allow_web=not args.no_web)

    def show(step: Step) -> None:
        if step.kind == "model":
            print(f"  [{step.model.split('/')[-1]}] {step.summary} ({step.prompt_tokens}+{step.completion_tokens} tok, "
                  f"{step.latency_s:.1f} s, ${step.cost_usd:.5f})")
        else:
            print(f"    -> {step.tool}({step.args}) : {step.summary}")

    turn = run_turn(ctx, llm, args.question, cfg, on_step=show)
    print("\nSENT TO THE MODEL:", turn.question_sent)
    print("\nANSWER (as the model wrote it):\n" + turn.answer)
    print("\nANSWER (as you see it):\n" + store.vault.restore(turn.answer))
    for s in turn.sources:
        print(f"  source: {s.title} - {s.url} ({s.label})")
    for d in store.drafts():
        print(f"\nDRAFT {d.id} to {store.vault.restore(d.to or '-')}: {d.title}\n{store.vault.restore(d.body)}")
    if turn.error:
        print("ERROR:", turn.error)
    print(f"\n{len(turn.steps)} steps, {turn.seconds} s, ${turn.cost_usd:.4f}; skills: {turn.skills_used}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
