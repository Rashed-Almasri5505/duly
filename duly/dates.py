"""Dates written in documents: how a date can be spelled, and whether a quote really contains it.

The reader model returns ISO dates; this module checks each one against the words of the document,
so a date the model computed or misread is flagged instead of trusted.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Literal

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
_SLASH_RE = re.compile(r"(?<!\d)(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})(?!\d)")


def normalize(text: str) -> str:
    """Lowercase, no table pipes or bold markers, single spaces, typographic dashes as '-'."""
    text = text.replace("|", " ").replace("*", " ").replace("–", "-").replace("—", "-")
    return " ".join(text.lower().split())


def slash_convention(text: str) -> Literal["dmy", "mdy"]:
    """Day-first unless a numeric date in the text can only be month-first (second number above 12)."""
    dmy = mdy = False
    for a, b, _ in _SLASH_RE.findall(text):
        if int(a) > 12:
            dmy = True
        elif int(b) > 12:
            mdy = True
    return "mdy" if mdy and not dmy else "dmy"


_MONTH = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
          r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_DMY_WORDS_RE = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?(?:\s+of)?\s+{_MONTH},?\s+(\d{{4}})\b", re.IGNORECASE)
_MDY_WORDS_RE = re.compile(rf"\b{_MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.IGNORECASE)
_ISO_RE = re.compile(r"(?<![\d/.\-])(\d{4})-(\d{1,2})-(\d{1,2})(?![\d/\-])")


def _month_number(name: str) -> int:
    return [m[:3].lower() for m in MONTHS].index(name[:3].lower()) + 1


def find_dates(text: str, convention: Literal["dmy", "mdy"] | None = None) -> list[tuple[date, str]]:
    """Every calendar date written in the text, with the line it is on, in order of appearance.

    Numeric dates follow the document's convention (day first unless it can only be month first).
    The same date on two different lines gives two entries, because it may mean two different things.
    """
    convention = convention or slash_convention(text)
    found: list[tuple[int, date, str]] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        hits: list[tuple[int, date]] = []
        for m in _ISO_RE.finditer(line):
            hits.append((m.start(), (int(m.group(1)), int(m.group(2)), int(m.group(3)))))
        for m in _SLASH_RE.finditer(line):
            a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            hits.append((m.start(), (y, b, a) if convention == "dmy" else (y, a, b)))
        for m in _DMY_WORDS_RE.finditer(line):
            hits.append((m.start(), (int(m.group(3)), _month_number(m.group(2)), int(m.group(1)))))
        for m in _MDY_WORDS_RE.finditer(line):
            hits.append((m.start(), (int(m.group(3)), _month_number(m.group(1)), int(m.group(2)))))
        for start, (y, mo, d) in hits:
            try:
                found.append((pos + start, date(y, mo, d), line.strip()))
            except ValueError:
                continue
        pos += len(line)
    seen: set[tuple[date, str]] = set()
    out: list[tuple[date, str]] = []
    for _, d, line in sorted(found, key=lambda f: f[0]):
        if (d, line) not in seen:
            seen.add((d, line))
            out.append((d, line))
    return out


def date_forms(d: date, convention: Literal["dmy", "mdy"] = "dmy") -> list[str]:
    """Ways a document may spell the date (lowercase)."""
    month, mon = MONTHS[d.month - 1], MONTHS[d.month - 1][:3]
    days = {str(d.day), f"{d.day:02d}"}
    forms = {d.isoformat(), f"{d.year}/{d.month:02d}/{d.day:02d}"}
    for dd in days:
        for mm in {str(d.month), f"{d.month:02d}"}:
            for sep in "/.-":
                forms.add(f"{dd}{sep}{mm}{sep}{d.year}" if convention == "dmy" else f"{mm}{sep}{dd}{sep}{d.year}")
        for m in (month, mon, mon + "."):
            forms |= {f"{dd} {m} {d.year}", f"{dd} {m}, {d.year}", f"{m} {dd}, {d.year}", f"{m} {dd} {d.year}",
                      f"{dd}-{m}-{d.year}"}
    suffix = "th" if 11 <= d.day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d.day % 10, "th")
    forms |= {f"{d.day}{suffix} {month} {d.year}", f"{d.day}{suffix} of {month} {d.year}",
              f"{month} {d.day}{suffix}, {d.year}"}
    return sorted(f.lower() for f in forms)


def date_in_text(d: date, text: str, convention: Literal["dmy", "mdy"] = "dmy") -> bool:
    norm = normalize(text)
    return any(re.search(rf"(?<![\w/.\-]){re.escape(f)}(?![\w/\-])", norm) for f in date_forms(d, convention))


def quote_in_text(quote: str, text: str) -> bool:
    """The quote appears word for word in the text (ignoring case, spacing and table markup)."""
    q = normalize(quote).strip(" .")
    return len(q) >= 8 and q in normalize(text)


NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine",
                10: "ten", 11: "eleven", 12: "twelve", 14: "fourteen", 15: "fifteen", 20: "twenty", 21: "twenty-one",
                28: "twenty-eight", 30: "thirty", 45: "forty-five", 60: "sixty", 90: "ninety"}


def number_in_text(n: int, text: str) -> bool:
    norm = normalize(text)
    word = NUMBER_WORDS.get(n)
    return re.search(rf"(?<!\d){n}(?!\d)", norm) is not None or (word is not None and word in norm)


def describe(d: date, today: date) -> str:
    """'Mon 23 Nov 2026 (in 24 days)' — the way dates are shown to the user."""
    delta = (d - today).days
    when = ("today" if delta == 0 else "tomorrow" if delta == 1 else "yesterday" if delta == -1
            else f"in {delta} days" if delta > 0 else f"{-delta} days ago")
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b %Y')} ({when})"


def add_days(d: date, days: int) -> date:
    return d + timedelta(days=days)
