"""Reads the four sample documents live with Nemotron Super, prints what it found and how each date was
checked, and saves the reading as the offline recording (samples/recorded_reading.json).

    python scripts/record_samples.py            # read and print only
    python scripts/record_samples.py --save     # also save the recording
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from duly.config import SAMPLES_DIR, load_config  # noqa: E402
from duly.dates import describe  # noqa: E402
from duly.llm import TokenFactory  # noqa: E402
from duly.reader import read_document  # noqa: E402
from duly.recorded import offsets_of, save_recording  # noqa: E402
from duly.samples import SAMPLE_PERSON, _front_matter, load_samples  # noqa: E402
from duly.store import Profile, Store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    llm = TokenFactory(cfg)
    today = date.today()
    store = Store(Path(tempfile.mkdtemp()))
    store.save_profile(Profile(name=SAMPLE_PERSON))
    readings = {}
    for sample in load_samples(today):
        doc = store.add_document(sample.title, sample.text, kind=sample.kind)
        doc = read_document(store, llm, doc, today, cfg)
        call = llm.calls[-1]
        print(f"\n=== {sample.title}: {doc.read_note} ({call.latency_s:.1f} s, {call.prompt_tokens}+"
              f"{call.completion_tokens} tokens, ${call.cost_usd:.5f})\n    {doc.summary}")
        items = [o for o in store.obligations() if o.doc_id == doc.id]
        for o in items:
            mark = "OK " if o.verified else "CHECK"
            print(f"  [{mark}] {o.kind:16s} {o.title} | due {describe(o.due_date, today)} | notice {o.notice_days} d"
                  f" | act by {o.act_by} | {o.status}" + (f" | {o.check_note}" if o.check_note else ""))
            print(f"          “{o.date_quote}”" + (f" + “{o.rule_quote}”" if o.rule_quote else ""))
        template = _front_matter((SAMPLES_DIR / f"{sample.slug}.md").read_text(encoding="utf-8"))[1]
        readings[sample.slug] = {"summary": doc.summary, "model": doc.read_by,
                                 "obligations": offsets_of(items, template, today)}
    total = sum(c.cost_usd for c in llm.calls)
    print(f"\nTotal: {len(llm.calls)} calls, ${total:.4f}")
    if args.save:
        save_recording(readings, today)
        print("saved samples/recorded_reading.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
