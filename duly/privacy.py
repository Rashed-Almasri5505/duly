"""Privacy firewall: personal details become placeholders before any text leaves this computer.

Detection is plain code that runs locally, so no model ever sees the raw text:

- patterns, with checksums where one exists: IBANs (mod 97), payment cards (Luhn), e-mail addresses,
  phone numbers, Saudi national ID and iqama numbers (10 digits starting with 1 or 2), vehicle VINs;
- labelled fields: the value after "Name:", "Policy number:", "Address:", "Serial number:" and similar,
  also in two-column Markdown tables;
- the people the user names in their profile (full names, and first and last names on their own);
- every value already in the vault, wherever it appears again.

Dates, amounts and company names are kept: the assistant needs them to reason about deadlines.

The vault maps each placeholder to its value, lives in the user's data folder, and puts the real values back
only in text shown on the user's screen. Before every outbound call, a leak check scans the whole payload for
any value in the vault, and the call is blocked if one is found.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KINDS: dict[str, str] = {
    "PERSON": "name",
    "NATIONAL_ID": "national ID, iqama or passport number",
    "BIRTHDATE": "date of birth",
    "PHONE": "phone number",
    "EMAIL": "e-mail address",
    "ADDRESS": "address",
    "IBAN": "IBAN",
    "CARD": "payment card number",
    "ACCOUNT": "policy, contract, account or member number",
    "SERIAL": "serial number or IMEI",
    "PLATE": "vehicle plate",
    "VIN": "vehicle identification number",
}
NUMERIC_KINDS = ("IBAN", "CARD", "PHONE", "NATIONAL_ID")

# Labels whose value is personal. Matched case-insensitively at the start of a line or table cell.
LABELS: dict[str, tuple[str, ...]] = {
    "PERSON": ("full name", "name", "customer name", "policyholder", "policy holder", "insured name",
               "insured", "tenant", "tenant name", "lessee", "landlord", "lessor", "member name",
               "account holder", "account name", "subscriber", "beneficiary", "driver", "owner", "client",
               "buyer", "sold to", "bill to", "emergency contact", "cardholder", "card holder"),
    "NATIONAL_ID": ("national id", "national id number", "id number", "id no", "iqama", "iqama number",
                    "iqama no", "passport", "passport number", "passport no", "civil id"),
    "BIRTHDATE": ("date of birth", "dob", "birth date"),
    "PHONE": ("phone", "mobile", "tel", "telephone", "phone number", "mobile number", "contact number"),
    "EMAIL": ("email", "e-mail", "email address"),
    "ADDRESS": ("address", "home address", "billing address", "property address", "premises",
                "unit address", "delivery address", "shipping address"),
    "IBAN": ("iban",),
    "ACCOUNT": ("policy number", "policy no", "contract number", "contract no", "agreement number",
                "agreement no", "account number", "account no", "customer id", "customer number", "customer no",
                "member id", "membership number", "membership no", "membership id", "invoice number",
                "invoice no", "order number", "order no", "reference", "reference number", "reference no",
                "ref", "lease number", "ejar contract number", "subscription id", "certificate number"),
    "SERIAL": ("serial number", "serial no", "s/n", "imei", "service tag"),
    "PLATE": ("plate number", "plate no", "vehicle plate", "plate"),
    "VIN": ("vin", "chassis number", "chassis no"),
}
_LABEL_TO_KIND = {label: kind for kind, labels in LABELS.items() for label in labels}
_LABEL_ALT = "|".join(sorted((re.escape(l) for l in _LABEL_TO_KIND), key=len, reverse=True))
# "Label: value" inside one line or table cell; list markers and ** around the label are allowed.
_LABEL_VALUE_RE = re.compile(rf"^[ \t>*\-•]*\**[ \t]*({_LABEL_ALT})[ \t]*\**[ \t]*(?:\([^)]*\))?[ \t]*[:：#][ \t]*\**"
                             rf"[ \t]*(.+?)[ \t]*\**[ \t]*$", re.IGNORECASE)
_LABEL_ONLY_RE = re.compile(rf"^[ \t*]*({_LABEL_ALT})[ \t*]*$", re.IGNORECASE)

EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
PHONE_INTL_RE = re.compile(r"(?<![\w+])\+\d{1,3}(?:[ \-.]?\(?\d{1,4}\)?){2,6}(?!\w)")
PHONE_SA_RE = re.compile(r"(?<![\d+])05\d(?:[ \-]?\d){7}(?!\d)")
IBAN_RE = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?(?![A-Z0-9])")
CARD_RE = re.compile(r"(?<![\d-])(?:\d[ \-]?){12,18}\d(?![\d-])")
NATIONAL_ID_RE = re.compile(r"(?<![\d+\-/.])[12]\d{9}(?![\d\-/.])")
VIN_RE = re.compile(r"(?<![A-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9])")
DEAR_RE = re.compile(r"\bDear[ \t]+(?:(?:Mr|Mrs|Ms|Miss|Dr|Eng)\.?[ \t]+)?([A-Z][a-zA-Z'\-]+(?:[ \t]+[A-Z][a-zA-Z'\-]+){0,3})")
DEAR_GENERIC = {"customer", "customers", "member", "members", "sir", "madam", "valued", "client", "tenant",
                "policyholder", "team", "all", "subscriber", "user", "colleague", "colleagues", "friend", "friends",
                "support", "service", "services", "care", "department", "office", "manager", "desk", "center",
                "centre", "helpdesk", "sales", "billing", "claims", "membership", "memberships", "hiring",
                "landlord", "owner", "applicant", "recipient", "whom", "partner", "partners"}
PLACEHOLDER_RE = re.compile(rf"\[?\b({'|'.join(KINDS)})[_ ](\d+)\b\]?")
# A value under a name label that is a company, not a person: kept, because companies are not personal data
# and the assistant needs them (whom to write to, whose procedure to look up).
COMPANY_RE = re.compile(r"\b(co|company|corp|corporation|inc|llc|ltd|limited|est|establishment|group|holding|bank|"
                        r"insurance|real estate|properties|trading|motors|electronics|club|fitness|telecom|"
                        r"services|agency|hospital|clinic|school|university)\b\.?", re.IGNORECASE)
_TRAILING = " \t.,;:)]}\"'*"
_KEEP = "KEEP"


class PrivacyError(RuntimeError):
    """A payload about to leave this computer still contains a value from the vault."""


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _number_key(kind: str, value: str) -> str:
    """Digits that identify a number; Saudi phone numbers match with or without +966 or the leading 0."""
    digits = _digits(value)
    if kind == "PHONE":
        digits = digits[3:] if digits.startswith("966") else digits
        digits = digits.lstrip("0")
    return digits


def iban_ok(candidate: str) -> bool:
    s = re.sub(r"\s", "", candidate).upper()
    if not 15 <= len(s) <= 34 or not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", s):
        return False
    number = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(number) % 97 == 1


def luhn_ok(candidate: str) -> bool:
    digits = _digits(candidate)
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d)
        if i % 2 == 1:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


@dataclass
class Span:
    start: int
    end: int
    kind: str
    value: str
    priority: int   # existing placeholders > vault values > labelled fields > checksummed patterns > others


def _clean_value(value: str) -> str:
    value = re.sub(r"\s*\((?:policyholder|tenant|member|owner|customer|insured|landlord)\)\s*$", "", value,
                   flags=re.IGNORECASE)
    return value.strip(_TRAILING)


def _personal(kind: str, value: str) -> bool:
    """A labelled value to hide: not empty, not already a placeholder, not a company under a name label."""
    if len(value) < 2 or PLACEHOLDER_RE.fullmatch(value):
        return False
    return not (kind == "PERSON" and COMPANY_RE.search(value))


def _label_spans(text: str) -> list[Span]:
    spans: list[Span] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        cells: list[tuple[int, str]] = []
        cursor = 0
        for part in re.split(r"(\||\t)", line):
            if part not in ("|", "\t"):
                cells.append((pos + cursor, part.rstrip("\r\n")))
            cursor += len(part)
        for i, (offset, cell) in enumerate(cells):
            m = _LABEL_VALUE_RE.match(cell)
            if m:
                kind, value = _LABEL_TO_KIND[m.group(1).lower()], _clean_value(m.group(2))
                if _personal(kind, value):
                    idx = cell.find(value, m.start(2))
                    start = offset + (idx if idx >= 0 else m.start(2))
                    spans.append(Span(start, start + len(value), kind, value, 3))
                continue
            m = _LABEL_ONLY_RE.match(cell)      # two-column table: | Name | Sami Nasser |
            if m and i + 1 < len(cells):
                nxt_offset, nxt = cells[i + 1]
                kind, value = _LABEL_TO_KIND[m.group(1).lower()], _clean_value(nxt)
                if _personal(kind, value) and not _LABEL_ONLY_RE.match(nxt) and not set(value) <= set("-: "):
                    start = nxt_offset + nxt.find(value)
                    spans.append(Span(start, start + len(value), kind, value, 3))
        pos += len(line)
    return spans


def _pattern_spans(text: str) -> list[Span]:
    spans: list[Span] = []
    for m in EMAIL_RE.finditer(text):      # above vault values, so a name inside an address stays one e-mail
        spans.append(Span(m.start(), m.end(), "EMAIL", m.group(), 5))
    for m in IBAN_RE.finditer(text):
        if iban_ok(m.group()):
            spans.append(Span(m.start(), m.end(), "IBAN", m.group(), 2))
    for m in CARD_RE.finditer(text):
        value = m.group().strip(" -")
        if luhn_ok(value):
            spans.append(Span(m.start(), m.start() + len(value), "CARD", value, 2))
    for rx in (PHONE_INTL_RE, PHONE_SA_RE):
        for m in rx.finditer(text):
            value = m.group().rstrip(" -.")
            if 8 <= len(_digits(value)) <= 15:
                spans.append(Span(m.start(), m.start() + len(value), "PHONE", value, 1))
    for m in NATIONAL_ID_RE.finditer(text):
        spans.append(Span(m.start(), m.end(), "NATIONAL_ID", m.group(), 1))
    for m in VIN_RE.finditer(text):
        v = m.group()
        if sum(c.isdigit() for c in v) >= 2 and sum(c.isalpha() for c in v) >= 2:
            spans.append(Span(m.start(), m.end(), "VIN", v, 1))
    for m in DEAR_RE.finditer(text):   # "Dear Dell Support" or "Dear Claims Team" is not a person
        words = {w.lower() for w in m.group(1).split()}
        if not words & DEAR_GENERIC and not COMPANY_RE.search(m.group(1)):
            spans.append(Span(m.start(1), m.end(1), "PERSON", m.group(1), 1))
    return spans


def _literal_spans(text: str, literals: list[tuple[str, str]]) -> list[Span]:
    """Every occurrence of known values (vault entries, profile names), whole words only."""
    spans: list[Span] = []
    for value, kind in literals:
        if len(value) < 3:
            continue
        pattern = re.escape(value).replace(r"\ ", r"\s+")
        for m in re.finditer(rf"(?<!\w){pattern}(?!\w)", text, flags=re.IGNORECASE):
            spans.append(Span(m.start(), m.end(), kind, value, 4))
    return spans


def _resolve(spans: list[Span]) -> list[Span]:
    """Non-overlapping spans: higher priority first, then longer."""
    chosen: list[Span] = []
    for s in sorted(spans, key=lambda s: (-s.priority, -(s.end - s.start), s.start)):
        if all(s.end <= c.start or s.start >= c.end for c in chosen):
            chosen.append(s)
    return sorted(chosen, key=lambda s: s.start)


def _digit_runs(text: str) -> list[str]:
    """Digits of each number in the text, with spaces, dashes and dots inside one number allowed."""
    return [_digits(m.group()) for m in re.finditer(r"\d(?:[ \-.]?\d)*", text)]


@dataclass
class VaultEntry:
    placeholder: str
    kind: str
    value: str
    aliases: list[str] = field(default_factory=list)
    source: str = ""

    def strings(self) -> list[str]:
        return [self.value, *self.aliases]


class Vault:
    """Placeholder <-> value map kept in the user's data folder. The same value always gets the same placeholder."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.entries: dict[str, VaultEntry] = {}
        self.counters: dict[str, int] = {}
        self.known_people: list[str] = []
        if path is not None and path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.entries = {e["placeholder"]: VaultEntry(**e) for e in data.get("entries", [])}
            self.counters = data.get("counters", {})
            self.known_people = data.get("known_people", [])

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {"entries": [e.__dict__ for e in self.entries.values()], "counters": self.counters,
                    "known_people": self.known_people}
            self.path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")

    def _find(self, kind: str, value: str) -> VaultEntry | None:
        numeric = kind in NUMERIC_KINDS
        key = _number_key(kind, value) if numeric else " ".join(value.lower().split())
        for e in self.entries.values():
            if numeric != (e.kind in NUMERIC_KINDS) or (numeric and e.kind != kind):
                continue
            for s in e.strings():
                other = _number_key(e.kind, s) if numeric else " ".join(s.lower().split())
                if other and other == key:
                    return e
        return None

    def placeholder_for(self, kind: str, value: str, source: str = "") -> str:
        with self._lock:
            found = self._find(kind, value)
            if found is not None:
                if value not in found.strings():
                    found.aliases.append(value)
                return found.placeholder
            n = self.counters.get(kind, 0) + 1
            self.counters[kind] = n
            placeholder = f"[{kind}_{n}]"
            self.entries[placeholder] = VaultEntry(placeholder, kind, value, [], source)
            return placeholder

    def lookup(self, kind: str, value: str) -> str | None:
        """The placeholder of a value already in the vault, without adding anything."""
        with self._lock:
            found = self._find(kind, value)
            return found.placeholder if found else None

    def add_known_person(self, full_name: str) -> str:
        """A person the user names in their profile: the full name and each part map to one placeholder."""
        full_name = " ".join(full_name.split())
        with self._lock:
            placeholder = self.placeholder_for("PERSON", full_name, source="your profile")
            entry = self.entries[placeholder]
            for part in full_name.split():
                if len(part) >= 3 and part not in entry.strings():
                    entry.aliases.append(part)
            if full_name not in self.known_people:
                self.known_people.append(full_name)
            return placeholder

    def redact(self, text: str, source: str = "") -> tuple[str, list[VaultEntry]]:
        """Replace every personal detail with its placeholder. Returns the safe text and the entries used."""
        with self._lock:
            keep = [Span(m.start(), m.end(), _KEEP, m.group(), 9) for m in PLACEHOLDER_RE.finditer(text)]
            literals = [(s, e.kind) for e in self.entries.values() for s in e.strings()]
            spans = _resolve(keep + _literal_spans(text, literals) + _label_spans(text) + _pattern_spans(text))
            out: list[str] = []
            used: dict[str, VaultEntry] = {}
            cursor = 0
            for s in spans:
                out.append(text[cursor:s.start])
                if s.kind == _KEEP:
                    out.append(s.value)
                else:
                    value = s.value if s.priority == 4 else text[s.start:s.end]
                    placeholder = self.placeholder_for(s.kind, value, source)
                    out.append(placeholder)
                    used[placeholder] = self.entries[placeholder]
                cursor = s.end
            out.append(text[cursor:])
            return "".join(out), list(used.values())

    def mask_known(self, text: str) -> str:
        """Replace only values already in the vault (for web results and tool output: public text is not
        scanned for new details, so a company's support number is not hidden)."""
        with self._lock:
            literals = [(s, e.kind) for e in self.entries.values() for s in e.strings()]
            spans = _resolve(_literal_spans(text, literals))
            out, cursor = [], 0
            for s in spans:
                out.append(text[cursor:s.start])
                out.append(self.placeholder_for(s.kind, s.value))
                cursor = s.end
            out.append(text[cursor:])
            return "".join(out)

    def restore(self, text: str) -> str:
        """Put the real values back (for the user's screen and saved drafts only)."""
        def sub(m: re.Match[str]) -> str:
            entry = self.entries.get(f"[{m.group(1)}_{m.group(2)}]")
            return entry.value if entry else m.group(0)
        return PLACEHOLDER_RE.sub(sub, text)

    def leaks(self, payload: str) -> list[str]:
        """Placeholders whose real value appears in the payload."""
        found: list[str] = []
        squashed = " ".join(payload.split()).lower()
        runs = _digit_runs(payload)
        for e in self.entries.values():
            for s in e.strings():
                if len(s) < 3:
                    continue
                if e.kind in NUMERIC_KINDS and len(_number_key(e.kind, s)) >= 7:
                    hit = any(_number_key(e.kind, s) in run for run in runs)
                else:
                    hit = re.search(rf"(?<!\w){re.escape(' '.join(s.lower().split()))}(?!\w)", squashed) is not None
                if hit:
                    found.append(e.placeholder)
                    break
        return found

    def check_outbound(self, payload: Any, destination: str) -> None:
        """Raise PrivacyError if anything about to go to `destination` contains a vault value."""
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
        leaked = self.leaks(text)
        if leaked:
            kinds = sorted({self.entries[p].kind for p in leaked})
            raise PrivacyError(f"blocked a call to {destination}: it contained a "
                               f"{', '.join(KINDS[k] for k in kinds)}")

    def masked(self, placeholder: str) -> str:
        """The value with most characters hidden, for the privacy panel."""
        value = self.entries[placeholder].value
        keep = 2 if len(value) > 6 else 1
        return "".join(c if (i < keep or not c.isalnum()) else "•" for i, c in enumerate(value))


def count_by_kind(entries: list[VaultEntry]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.kind] = counts.get(e.kind, 0) + 1
    return counts
