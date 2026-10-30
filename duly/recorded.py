"""Recorded readings of the sample documents, used when live AI is unavailable (no key, or a budget cap).

scripts/record_samples.py reads the samples live with Nemotron and saves the result with every date stored
relative to the day it was recorded, so the recording stays correct for the relative-dated samples on any
later day. The app labels these items as recorded, with the recording date.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from duly.config import SAMPLES_DIR
from duly.samples import TOKEN_RE, render
from duly.store import Obligation, new_id

RECORDING = SAMPLES_DIR / "recorded_reading.json"


def templatize(text: str, template: str, today: date) -> str:
    """Replace each rendered sample date in text with the template token that produced it."""
    for m in TOKEN_RE.finditer(template):
        rendered = (today + timedelta(days=int(m.group(1)))).strftime(m.group(2) or "%d %B %Y")
        text = text.replace(rendered, m.group(0))
    return text


def save_recording(readings: dict[str, Any], recorded_on: date, path: Path = RECORDING) -> None:
    path.write_text(json.dumps({"recorded_on": recorded_on.isoformat(), "samples": readings}, indent=1,
                               ensure_ascii=False), encoding="utf-8")


def load_recording(path: Path = RECORDING) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def recorded_obligations(slug: str, doc_id: str, today: date, recording: dict[str, Any]) -> tuple[str | None, list[Obligation]]:
    """The recorded summary and obligations of one sample, re-dated for today."""
    entry = recording["samples"].get(slug)
    if not entry:
        return None, []
    items = []
    for raw in entry["obligations"]:
        o = dict(raw)
        o["due_date"] = today + timedelta(days=int(o.pop("due_offset_days")))
        for key in ("date_quote", "rule_quote", "title", "action"):
            if o.get(key):
                o[key] = render(o[key], today)
        items.append(Obligation(**o, id=new_id("obl"), doc_id=doc_id))
    return entry.get("summary"), items


def offsets_of(items: list[Obligation], template: str, today: date) -> list[dict[str, Any]]:
    out = []
    for o in items:
        d = o.model_dump(exclude={"id", "doc_id"})
        d["due_offset_days"] = (o.due_date - today).days
        del d["due_date"]
        for key in ("date_quote", "rule_quote", "title", "action"):
            if d.get(key):
                d[key] = templatize(d[key], template, today)
        out.append(d)
    return out


_ = re  # keeps the import list stable for tools that strip unused imports
