"""Measures Duly on the fictional samples, live, and writes eval/results.md and eval/results.json.

Reader: each sample is read several times; a deadline counts as found only if both its date and its notice
period match the answer key below (written by hand from the documents). Assistant: each suggested question is
asked several times; we check the skill Nano picked, that the skill's required tools were really called, that no
call was blocked by the privacy check, and how many links or addresses had to be removed.

    python scripts/evaluate.py --reads 3 --asks 2          # about $0.10
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from duly.agent import run_turn  # noqa: E402
from duly.config import REPO_ROOT, load_config  # noqa: E402
from duly.gateway import TavilySearch  # noqa: E402
from duly.llm import TokenFactory  # noqa: E402
from duly.reader import read_document  # noqa: E402
from duly.samples import SAMPLE_PERSON, install_samples, load_samples  # noqa: E402
from duly.skills import load_skills  # noqa: E402
from duly.store import Profile, Store  # noqa: E402
from duly.tools import Context  # noqa: E402

# Answer key: (days from today to the due date, notice days) for every date the owner must act on.
KEY = {
    "01_laptop_receipt": {(65, 0)},
    "02_car_insurance": {(30, 7)},
    "03_gym_membership": {(24, 14)},
    "04_apartment_lease": {(12, 0), (103, 0), (195, 60)},
}
QUESTIONS = {
    "My laptop screen started flickering. Am I still covered?": "warranty-claim",
    "I don't want to renew the gym. What do I do, and by when?": "cancel-before-renewal",
    "What do I need to do in the next 30 days?": "month-ahead",
    "How do I renew my car insurance without a gap?": "renewal-checklist",
}


def eval_reader(cfg, llm, today: date, runs: int) -> dict:
    found = expected = extra = unverified = 0
    latencies, costs = [], []
    for _ in range(runs):
        store = Store(Path(tempfile.mkdtemp()))
        store.save_profile(Profile(name=SAMPLE_PERSON))
        for s in load_samples(today):
            doc = store.add_document(s.title, s.text, kind=s.kind)
            read_document(store, llm, doc, today, cfg)
            call = llm.calls[-1]
            latencies.append(call.latency_s)
            costs.append(call.cost_usd)
            got = {((o.due_date - today).days, o.notice_days) for o in store.obligations() if o.doc_id == doc.id}
            key = KEY[s.slug]
            expected += len(key)
            found += len(key & got)
            extra += len(got - key)
            unverified += sum(not o.verified for o in store.obligations() if o.doc_id == doc.id)
    return {"runs": runs, "documents_read": runs * len(KEY), "deadlines_expected": expected,
            "deadlines_found": found, "extra_items": extra, "items_flagged_unverified": unverified,
            "median_latency_s": statistics.median(latencies), "mean_cost_usd": statistics.mean(costs)}


def eval_assistant(cfg, llm, today: date, runs: int) -> dict:
    rows = []
    skills = load_skills()
    for q, want in QUESTIONS.items():
        for _ in range(runs):
            store = Store(Path(tempfile.mkdtemp()))
            install_samples(store, today)
            ctx = Context(store=store, today=today, skills=skills, web=TavilySearch(cfg))
            turn = run_turn(ctx, llm, q, cfg)
            called = {s.tool for s in turn.steps if s.kind == "tool" and s.tool}
            required = set(skills[want].requires_tools)
            rows.append({"question": q, "expected_skill": want, "skill": (turn.skills_used or [None])[0],
                         "required_tools_called": required <= called, "nudges": sum(s.label == "skill check" for s in turn.steps),
                         "blocked_calls": sum(r.check == "blocked" for r in store.outbound()),
                         "outbound_calls": len(store.outbound()), "removed": len(turn.removed),
                         "error": turn.error, "seconds": turn.seconds, "cost_usd": round(turn.cost_usd, 5),
                         "answered": bool(turn.answer) and not turn.error})
    return {"runs_per_question": runs, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reads", type=int, default=3)
    ap.add_argument("--asks", type=int, default=2)
    args = ap.parse_args()
    cfg = load_config()
    llm = TokenFactory(cfg)
    today = date.today()
    reader = eval_reader(cfg, llm, today, args.reads)
    assistant = eval_assistant(cfg, llm, today, args.asks)
    out = REPO_ROOT / "eval"
    out.mkdir(exist_ok=True)
    result = {"date": today.isoformat(), "models": cfg["llm"]["models"], "reader": reader, "assistant": assistant,
              "total_cost_usd": round(sum(c.cost_usd for c in llm.calls), 4), "model_calls": len(llm.calls)}
    (out / "results.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    rows = assistant["rows"]
    lines = [f"# Duly evaluation, {today.isoformat()}", "",
             "Fictional sample documents, live calls to Nebius Token Factory. Reproduce with `python scripts/evaluate.py`.", "",
             "## Reading documents (Nemotron Super, dates found by code)", "",
             "| Measure | Result |", "|---|---|",
             f"| Documents read | {reader['documents_read']} ({args.reads} runs over 4 samples) |",
             f"| Deadlines found with the right date and notice period | {reader['deadlines_found']} of {reader['deadlines_expected']} |",
             f"| Extra items (not in the answer key) | {reader['extra_items']} |",
             f"| Items flagged \"check this\" | {reader['items_flagged_unverified']} |",
             f"| Median latency per document | {reader['median_latency_s']:.1f} s |",
             f"| Mean cost per document | ${reader['mean_cost_usd']:.5f} |", "",
             "## Answering questions (Nemotron Nano picks the skill, Nemotron Super acts)", "",
             "| Question | Runs | Right skill | Required tools used | Nudged | Blocked calls | Links removed | Median time | Mean cost |",
             "|---|---|---|---|---|---|---|---|---|"]
    for q, want in QUESTIONS.items():
        rs = [r for r in rows if r["question"] == q]
        lines.append(f"| {q} | {len(rs)} | {sum(r['skill'] == want for r in rs)}/{len(rs)} | "
                     f"{sum(r['required_tools_called'] for r in rs)}/{len(rs)} | {sum(r['nudges'] > 0 for r in rs)} | "
                     f"{sum(r['blocked_calls'] for r in rs)} | {sum(r['removed'] for r in rs)} | "
                     f"{statistics.median(r['seconds'] for r in rs):.0f} s | ${statistics.mean(r['cost_usd'] for r in rs):.4f} |")
    lines += ["", f"Total for this evaluation: {result['model_calls']} model calls, ${result['total_cost_usd']:.4f}.",
              "\"Nudged\" counts runs where Duly had to ask the model for a step its skill requires before it could answer."]
    (out / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
