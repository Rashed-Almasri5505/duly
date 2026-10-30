"""Fictional sample documents for the demo, dated relative to today.

A token like {{D+24|%B %d, %Y}} becomes the date 24 days from today in that format, so the demo always has
upcoming deadlines, whenever it is opened. Every name, number and address in the samples is invented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from duly.config import SAMPLES_DIR

if TYPE_CHECKING:
    from duly.store import Store

TOKEN_RE = re.compile(r"\{\{D([+-]\d+)(?:\|([^}]+))?\}\}")
SAMPLE_PERSON = "Sami Nasser"     # the fictional owner of the sample documents


@dataclass
class Sample:
    slug: str
    title: str
    kind: str
    text: str


def render(template: str, today: date) -> str:
    def sub(m: re.Match[str]) -> str:
        d = today + timedelta(days=int(m.group(1)))
        fmt = m.group(2) or "%d %B %Y"
        return d.strftime(fmt)
    return TOKEN_RE.sub(sub, template)


def _front_matter(raw: str) -> tuple[dict[str, str], str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, flags=re.DOTALL)
    if not m:
        return {}, raw
    meta = dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
    return {k.strip(): v.strip() for k, v in meta.items()}, raw[m.end():]


def install_samples(store: "Store", today: date) -> int:
    """Put the fictional samples into a store, with the recorded Nemotron reading re-dated for today.
    Returns how many documents were added."""
    from duly.recorded import load_recording, recorded_obligations
    from duly.store import Profile

    profile = store.profile()
    if not profile.name:                      # the demo visitor plays the fictional owner of the samples
        profile.name = SAMPLE_PERSON
    elif SAMPLE_PERSON not in profile.people:  # a real user trying the samples: still hide the sample name
        profile.people.append(SAMPLE_PERSON)
    store.save_profile(Profile(**profile.model_dump()))
    recording = load_recording()
    samples = load_samples(today)
    for s in samples:
        doc = store.add_document(s.title, s.text, kind=s.kind, filename=f"{s.slug}.md")
        if recording:
            summary, items = recorded_obligations(s.slug, doc.id, today, recording)
            store.set_obligations(doc.id, items)
            doc.summary, doc.read_status = summary, "recorded"
            doc.read_by = recording["samples"].get(s.slug, {}).get("model")
            doc.read_note = f"saved reading from {recording['recorded_on']}"
            store.update_document(doc)
    return len(samples)


def load_samples(today: date, folder: Path = SAMPLES_DIR) -> list[Sample]:
    samples = []
    for path in sorted(folder.glob("*.md")):
        meta, body = _front_matter(path.read_text(encoding="utf-8"))
        samples.append(Sample(path.stem, meta.get("title", path.stem), meta.get("kind", "other"),
                              render(body, today).strip() + "\n"))
    return samples
