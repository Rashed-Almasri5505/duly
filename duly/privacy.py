"""Privacy firewall: personal details become placeholders before any text leaves this computer.

Detection is plain code that runs locally, so no model ever sees the raw text:

- patterns, with checksums where one exists: IBANs (mod 97), payment cards (Luhn), e-mail addresses, phone numbers
  (international, 00 and 966 prefixes, Saudi mobiles and landlines), Saudi national ID and iqama numbers (10 digits
  starting with 1 or 2), vehicle VINs;
- labelled fields in English and Arabic: the value after "Name:", "Policy No.:", "D.O.B.", "Address" ... on the same
  line (also without a colon, as text copied from PDF forms reads), on the lines below a label that stands alone,
  in two-column tables, and in the columns of tables whose header row holds labels;
- numbers and dates named in sentences: "my passport number is ...", "policy MTR-...", "born on ...", street
  addresses with a house number, Saudi short addresses;
- the people the user names in their profile, and every value already in the vault wherever it appears again,
  including the first or last name alone of any person found, and account numbers grouped differently.

Arabic-Indic digits are read as digits. Dates, amounts and company names are kept: the assistant needs them.

Redaction runs in two passes: the first finds and registers every value, the second replaces every occurrence of
every known value, so a detail repeated later in the same text is hidden too. The vault maps each placeholder to its
value, lives in the user's data folder, and puts the real values back only on the user's screen. Before every
outbound call, a leak check scans the whole payload for any value in the vault and blocks the call if one is found.
What the detectors miss cannot be blocked: a name with no label that is not in the profile is the main example.
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
NUMERIC_KINDS = ("IBAN", "CARD", "PHONE", "NATIONAL_ID")      # compared on their digits
ALNUM_KINDS = ("ACCOUNT", "SERIAL", "PLATE", "VIN")            # compared on letters and digits, any grouping
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

# Labels whose value is personal, matched case-insensitively at the start of a line or table cell.
LABELS: dict[str, tuple[str, ...]] = {
    "PERSON": ("full name", "name", "customer", "customer name", "policyholder", "policy holder", "insured name",
               "insured", "insured person", "name of insured", "tenant", "tenant name", "lessee", "landlord",
               "lessor", "member name", "account holder", "account name", "subscriber", "beneficiary", "driver",
               "named driver", "owner", "client", "member", "buyer", "sold to", "bill to", "billed to", "ship to",
               "deliver to", "emergency contact", "cardholder", "card holder", "patient", "student", "employee",
               "applicant", "guest", "passenger", "spouse", "dependant", "dependent", "next of kin", "attention",
               "attn", "signed by", "الاسم", "اسم العميل", "اسم المؤمن له", "حامل الوثيقة", "المستأجر", "المؤجر",
               "اسم المستأجر", "صاحب الحساب"),
    "NATIONAL_ID": ("national id", "national id number", "id number", "iqama", "iqama number", "passport",
                    "passport number", "civil id", "residence permit", "رقم الهوية", "الهوية الوطنية",
                    "رقم الإقامة", "رقم الاقامة", "رقم الجواز", "رقم جواز السفر", "رقم السجل المدني"),
    "BIRTHDATE": ("date of birth", "dob", "birth date", "birthday", "تاريخ الميلاد"),
    "PHONE": ("phone", "mobile", "tel", "telephone", "phone number", "mobile number", "contact number", "fax",
              "landline", "home phone", "work phone", "رقم الجوال", "الجوال", "رقم الهاتف", "الهاتف"),
    "EMAIL": ("email", "e-mail", "email address", "البريد الإلكتروني", "البريد الالكتروني"),
    "ADDRESS": ("address", "home address", "billing address", "property address", "premises", "unit address",
                "delivery address", "shipping address", "national address", "العنوان", "العنوان الوطني",
                "عنوان السكن"),
    "IBAN": ("iban", "الآيبان", "رقم الآيبان", "آيبان", "ايبان"),
    "ACCOUNT": ("policy number", "contract number", "agreement number", "account number", "customer id",
                "customer number", "member id", "membership number", "membership id", "invoice number",
                "order number", "reference", "reference number", "ref", "lease number", "ejar contract number",
                "subscription id", "certificate number", "claim number", "booking number", "رقم الحساب",
                "رقم الوثيقة", "رقم العقد", "رقم العضوية", "رقم الفاتورة"),
    "SERIAL": ("serial number", "s/n", "imei", "service tag"),
    "PLATE": ("plate number", "vehicle plate", "plate", "registration number", "رقم اللوحة", "اللوحة"),
    "VIN": ("vin", "chassis number", "رقم الهيكل"),
}
# Nouns that name a number when followed by "No.", "Nr.", "Number" or "#": "Policy No.: ...", "Tel. No. ...".
NUMBER_NOUNS: dict[str, str] = {
    **{n: "ACCOUNT" for n in ("policy", "contract", "agreement", "account", "acct", "invoice", "order", "member",
                              "membership", "customer", "client", "reference", "ref", "lease", "ejar contract",
                              "ejar", "subscription", "certificate", "claim", "booking", "ticket", "receipt",
                              "transaction", "file", "case")},
    **{n: "NATIONAL_ID" for n in ("passport", "id", "national id", "iqama", "civil id", "residence permit",
                                  "border")},
    **{n: "PHONE" for n in ("tel", "phone", "telephone", "mobile", "contact", "fax")},
    **{n: "PLATE" for n in ("plate", "vehicle plate", "registration")},
    **{n: "SERIAL" for n in ("serial", "imei")},
    **{n: "VIN" for n in ("chassis", "vin")},
}
_LABEL_TO_KIND = {label: kind for kind, labels in LABELS.items() for label in labels}


def _phrase_re(phrase: str) -> str:
    if phrase == "dob":
        return r"d\.?[ \t]?o\.?[ \t]?b\.?"
    return r"\.?[ \t]+".join(re.escape(w) for w in phrase.split()) + r"\.?"


_PHRASES = sorted(set(_LABEL_TO_KIND) | set(NUMBER_NOUNS), key=len, reverse=True)
_PHRASE_ALT = "|".join(f"(?:{_phrase_re(p)})" for p in _PHRASES)
_SUFFIX = r"[ \t]*(?:no|nr|num|number)\b\.?|[ \t]*#"
# A label at the start of a line or cell, its "No." suffix, a parenthesis, a separator, and the value.
_LINE_RE = re.compile(
    rf"^(?P<pre>[ \t>*\-•]*\**[ \t]*)(?P<label>{_PHRASE_ALT})(?![A-Za-z؀-ۿ])(?P<suffix>{_SUFFIX})?"
    rf"(?P<paren>[ \t]*\([^)\n]*\))?(?P<sep>[ \t]*\**[ \t]*[:：=][ \t]*\**[ \t]*|[ \t]+)?(?P<value>.*?)[ \t]*\**[ \t]*$",
    re.IGNORECASE)

EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
PHONE_RES = [
    re.compile(r"(?<![\w+])\+\d{1,3}(?:[ \-.]?\(?\d{1,4}\)?){2,6}(?!\w)"),            # +966 50 555 0134
    re.compile(r"(?<![\d+])00\d{1,3}(?:[ \-.]?\(?\d{1,4}\)?){2,6}(?!\d)"),            # 00966 50 ..., 0044 20 ...
    re.compile(r"(?<![\d+])966[ \-.]?(?:5\d|1[1-7])(?:[ \-.]?\d){7}(?!\d)"),         # 966501234567
    re.compile(r"(?<![\d+])\(?05\d\)?(?:[ \-.]?\d){7}(?!\d)"),                       # 050 555 0134, (050) 555 0134
    re.compile(r"(?<![\d+])\(?01[1-7]\)?(?:[ \-.]?\d){7}(?!\d)"),                    # 013 860 1234 (landline)
    re.compile(r"(?<![\d+.])5\d[ \-.]\d{3}[ \-.]\d{4}(?!\d)"),                       # 50 123 4567
    re.compile(r"(?<![\d+])\(\d{3}\)[ \-.]?\d{3}[ \-.]\d{4}(?!\d)"),                 # (415) 555-0123
]
IBAN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2}[ ]?\d{2}(?:[ \-]?[A-Za-z0-9]{4}){2,7}(?:[ \-]?[A-Za-z0-9]{1,4})?"
                     r"(?![A-Za-z0-9])")
CARD_RE = re.compile(r"(?<![\d\-])(?<!\d\.)(?:\d[ \-.]?){12,18}\d(?![\d\-]|\.\d)")
NATIONAL_ID_RE = re.compile(r"(?<![\d+\-/])(?<!\d\.)[12]\d{9}(?![\d\-/]|\.\d)")
VIN_RE = re.compile(r"(?<![A-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9])")
DEAR_RE = re.compile(r"\bDear[ \t]+(?:(?:Mr|Mrs|Ms|Miss|Dr|Eng)\.?[ \t]+)?([A-Z][a-zA-Z'\-]+(?:[ \t]+[A-Z][a-zA-Z'\-]+){0,3})")
DEAR_GENERIC = {"customer", "customers", "member", "members", "sir", "madam", "valued", "client", "tenant",
                "policyholder", "team", "all", "subscriber", "user", "colleague", "colleagues", "friend", "friends",
                "support", "service", "services", "care", "department", "office", "manager", "desk", "center",
                "centre", "helpdesk", "sales", "billing", "claims", "membership", "memberships", "hiring",
                "landlord", "owner", "applicant", "recipient", "whom", "partner", "partners"}
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
          r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_DATE = (rf"\d{{1,2}}[/.\-]\d{{1,2}}[/.\-]\d{{2,4}}|\d{{4}}[/.\-]\d{{1,2}}[/.\-]\d{{1,2}}|"
         rf"\d{{1,2}}(?:st|nd|rd|th)?[ \t]+(?:of[ \t]+)?{_MONTH},?[ \t]+\d{{4}}|{_MONTH}[ \t]+\d{{1,2}}(?:st|nd|rd|th)?,?[ \t]+\d{{4}}")
# Numbers and dates named in a sentence.
CODE_CONTEXT_RE = re.compile(
    r"\b(?P<noun>passport|policy|account|acct|contract|membership|member|iqama|national id|id|reference|ref|serial|"
    r"chassis|vin|invoice|claim|booking|order|customer|subscription|ticket|certificate|lease|ejar)\b"
    r"(?:[ \t]+(?:number|no\.?|num\.?|nr\.?)|[ \t]*#)?(?:[ \t]+(?:is|was|=)|[ \t]*[:=])?[ \t]*"
    r"(?P<code>(?=[A-Za-z0-9\-/]*\d)[A-Za-z0-9][A-Za-z0-9\-/]{2,}[A-Za-z0-9])", re.IGNORECASE)
PLATE_CONTEXT_RE = re.compile(
    r"\bplate\b(?:[ \t]+(?:number|no\.?))?(?:[ \t]+(?:is|was))?[ \t]*[:#]?[ \t]*"
    r"(?P<code>[A-Za-z]{1,3}[ \t]?\d{1,4}|\d{1,4}[ \t]?[A-Za-z]{1,3})\b", re.IGNORECASE)
BIRTH_CONTEXT_RE = re.compile(
    rf"\b(?:born|date[ \t]+of[ \t]+birth|birth[ \t]?date|birthday|d\.?o\.?b\.?)(?:[ \t]+(?:on|in|is|was))?[ \t]*:?[ \t]*"
    rf"(?P<date>{_DATE})", re.IGNORECASE)
STREET_RE = re.compile(
    r"\b\d{1,5}[ \t]+(?:[A-Z][\w'’\-]*[ \t]+){0,4}(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Lane|Ln|Drive|Dr|"
    r"Way|Highway|Hwy|Square|Sq)\b\.?(?:,[ \t]*(?:[A-Z][\w'’\-]*[ \t]?){1,4})*(?:[ \t]*\d{5})?")
SHORT_ADDRESS_RE = re.compile(r"\b[A-Z]{4}\d{4}\b")          # Saudi National Address short code

PLACEHOLDER_RE = re.compile(rf"\[?\b({'|'.join(KINDS)})[_ ](\d+)\b\]?")      # tolerant, for restoring
_STRICT_PLACEHOLDER_RE = re.compile(rf"\[({'|'.join(KINDS)})_(\d+)\]")       # what redaction leaves alone
# A value under a name label that is a company, not a person: kept, because companies are not personal data
# and the assistant needs them (whom to write to, whose procedure to look up).
COMPANY_RE = re.compile(r"\b(co|company|corp|corporation|inc|llc|ltd|limited|est|establishment|group|holding|bank|"
                        r"insurance|real estate|properties|trading|motors|electronics|club|fitness|telecom|"
                        r"services|agency|hospital|clinic|school|university)\b\.?", re.IGNORECASE)
_NAME_PARTICLES = {"al", "el", "bin", "bint", "ibn", "abu", "abd", "de", "da", "van", "von", "la", "le", "mr", "mrs",
                   "ms", "miss", "dr", "eng", "prof", "sir", "the", "and", "of"}
_NAME_WORD = r"(?:[A-Z][a-zA-Z'’\-]+|al|el|bin|bint|ibn|abu|abd|de|da|van|von|Al-[A-Z][a-z]+)"
_NAME_SHAPE_RE = re.compile(rf"^(?:(?:Mr|Mrs|Ms|Miss|Dr|Eng)\.?[ \t]+)?{_NAME_WORD}(?:[ \t]+{_NAME_WORD}){{1,4}}$")
_ARABIC_NAME_RE = re.compile(r"^[؀-ۿ]+(?:[ \t]+[؀-ۿ]+){1,4}$")
_TRAILING = " \t.,;:)]}\"'*"
_KEEP = "KEEP"


class PrivacyError(RuntimeError):
    """A payload about to leave this computer still contains a value from the vault."""


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text.translate(ARABIC_DIGITS))


def _alnum(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.translate(ARABIC_DIGITS).lower())


def _number_key(kind: str, value: str) -> str:
    """What identifies a number: Saudi phone numbers match with or without +966, 00966 or the leading 0;
    account, serial and plate numbers match on their letters and digits, however they are grouped."""
    if kind in ALNUM_KINDS:
        return _alnum(value)
    digits = _digits(value)
    if kind == "PHONE":
        digits = digits[5:] if digits.startswith("00966") else digits[3:] if digits.startswith("966") else digits
        digits = digits.lstrip("0")
    return digits


def iban_ok(candidate: str) -> bool:
    s = re.sub(r"[\s\-]", "", candidate).upper()
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
    priority: int   # placeholders > e-mails > vault values > labelled fields > checksummed patterns > others
    placeholder: str | None = None      # for vault values: the entry they belong to


def _norm_label(text: str) -> str:
    norm = " ".join(re.sub(r"\.", " ", text.lower()).split())
    return "dob" if re.fullmatch(r"d ?o ?b", norm) else norm


def _clean_value(value: str) -> str:
    value = re.split(r"\t|[ ]{2,}(?=[^\s:：]{1,30}(?:[ ][^\s:：]{1,30}){0,3}[ ]?[:：])", value, maxsplit=1)[0]
    value = re.sub(r"\s*\((?:policyholder|tenant|member|owner|customer|insured|landlord)\)\s*$", "", value,
                   flags=re.IGNORECASE)
    return value.strip(_TRAILING)


def _name_like(value: str) -> bool:
    return bool(_NAME_SHAPE_RE.match(value) or _ARABIC_NAME_RE.match(value)) and len(value) <= 60


def _too_short(kind: str, value: str) -> bool:
    """A number too short to identify anyone ("Reference: 2026"): hiding it would hide every date with 2026 in it."""
    if kind not in NUMERIC_KINDS + ALNUM_KINDS:
        return False
    key = _alnum(value)
    return len(key) < 5 and not (len(key) >= 4 and re.search(r"[a-z]", key) and re.search(r"\d", key))


def _plausible(kind: str, value: str, strict: bool) -> bool:
    """A value worth hiding. Strict applies where there is no colon (PDF forms, the line below a label, table
    columns): there it must look like its kind, so a heading such as "Address changes" is left alone."""
    if len(value) < 2 or _STRICT_PLACEHOLDER_RE.fullmatch(value) or set(value) <= set("-–—: |"):
        return False
    if kind == "PERSON" and COMPANY_RE.search(value):
        return False
    if _too_short(kind, value):
        return False
    if not strict:
        return True
    if ":" in value or "：" in value:                 # another label follows: this is not a value
        return False
    has_digit = any(c.isdigit() for c in value.translate(ARABIC_DIGITS))
    if kind == "PERSON":
        return _name_like(value)
    if kind == "EMAIL":
        return "@" in value
    if kind == "ADDRESS":
        return has_digit and len(value) <= 160
    return has_digit and len(value) <= 40 and len(value.split()) <= 4


def _label_match(cell: str) -> tuple[str, str, re.Match[str]] | None:
    """(kind, mode, match) for a cell that starts with a label. Modes: colon, number (a "No." suffix, no colon),
    form (whitespace only), alone (nothing after the label)."""
    m = _LINE_RE.match(cell)
    if not m:
        return None
    label, suffix = _norm_label(m.group("label")), bool(m.group("suffix"))
    sep, value = m.group("sep") or "", m.group("value")
    if suffix and label in NUMBER_NOUNS:
        kind = NUMBER_NOUNS[label]
    elif label in _LABEL_TO_KIND:
        kind = _LABEL_TO_KIND[label]
    else:
        return None
    if not value.strip():
        return kind, "alone", m
    if re.search(r"[:：=]", sep):
        return kind, "colon", m
    if suffix:
        return kind, "number", m
    if sep:
        return kind, "form", m
    return None


def _lines(text: str) -> list[tuple[int, str]]:
    out, pos = [], 0
    for line in text.splitlines(keepends=True):
        out.append((pos, line.rstrip("\r\n")))
        pos += len(line)
    return out


def _cells(offset: int, line: str) -> list[tuple[int, str]]:
    cells, cursor = [], 0
    for part in re.split(r"(\||\t)", line):
        if part not in ("|", "\t"):
            cells.append((offset + cursor, part))
        cursor += len(part)
    return cells


def _span_in(offset: int, cell: str, value: str, start_at: int, kind: str, priority: int = 3) -> Span:
    idx = cell.find(value, start_at)
    start = offset + (idx if idx >= 0 else start_at)
    return Span(start, start + len(value), kind, value, priority)


def _label_spans(text: str) -> list[Span]:
    spans: list[Span] = []
    lines = _lines(text)
    for li, (offset, line) in enumerate(lines):
        cells = _cells(offset, line)
        for i, (c_off, cell) in enumerate(cells):
            found = _label_match(cell)
            if found is None:
                continue
            kind, mode, m = found
            if mode != "alone":
                value = _clean_value(m.group("value"))
                if _plausible(kind, value, strict=mode in ("form", "number")):
                    spans.append(_span_in(c_off, cell, value, m.start("value"), kind))
                continue
            if i + 1 < len(cells):                     # two-column table: | Name | Sami Nasser |
                n_off, nxt = cells[i + 1]
                value = _clean_value(nxt)
                if _label_match(nxt) is None and _plausible(kind, value, strict=False):
                    spans.append(_span_in(n_off, nxt, value, 0, kind))
            elif len(cells) == 1:                      # the label stands alone: its value is on the lines below
                spans.extend(_below(lines, li, kind))
    spans.extend(_table_column_spans(lines))
    return spans


def _below(lines: list[tuple[int, str]], li: int, kind: str) -> list[Span]:
    """Values on the lines under a label that stands alone ("Bill To:" then a name and an address)."""
    block: list[tuple[int, str]] = []
    for offset, line in lines[li + 1: li + 6]:
        if not line.strip() or _label_match(line) is not None or re.search(r"[:：]\s*\S", line):
            break
        block.append((offset, line))
    if not block:
        return []
    spans: list[Span] = []

    def whole(rows: list[tuple[int, str]], k: str) -> None:
        first_off, first = rows[0]
        last_off, last = rows[-1]
        start = first_off + len(first) - len(first.lstrip())
        value_end = last_off + len(last.rstrip(_TRAILING + " "))
        if value_end > start:
            spans.append(Span(start, value_end, k, "", 3))

    if kind == "PERSON":
        name = block[0][1].strip(_TRAILING)
        if _plausible("PERSON", name, strict=True) or (not any(c.isdigit() for c in name) and len(name.split()) <= 5):
            whole(block[:1], "PERSON")
            rest = block[1:4]
            if rest and any(any(c.isdigit() for c in r[1]) for r in rest):
                whole(rest, "ADDRESS")
    elif kind == "ADDRESS":
        whole(block[:4], "ADDRESS")
    else:
        value = block[0][1].strip(_TRAILING)
        if _plausible(kind, value, strict=True):
            whole(block[:1], kind)
    return spans


def _table_column_spans(lines: list[tuple[int, str]]) -> list[Span]:
    """Tables whose header row holds labels: each header's kind applies to the cells in its column."""
    spans: list[Span] = []
    i = 0
    while i < len(lines):
        offset, line = lines[i]
        sep = "|" if line.strip().startswith("|") else "\t" if "\t" in line else None
        if sep is None:
            i += 1
            continue
        block = [lines[i]]
        j = i + 1
        while j < len(lines) and (lines[j][1].strip().startswith("|") if sep == "|" else "\t" in lines[j][1]):
            block.append(lines[j])
            j += 1
        i = j
        if len(block) < 2:
            continue
        header = _cells(*block[0])
        kinds: dict[int, str] = {}
        for k, (_, cell) in enumerate(header):
            found = _label_match(cell)
            if found is not None and found[1] == "alone":
                kinds[k] = found[0]
        if not kinds:
            continue
        for row_off, row in block[1:]:
            cells = _cells(row_off, row)
            if all(set(c.strip()) <= set("-: ") for _, c in cells):
                continue                                   # the |---|---| separator row
            for k, kind in kinds.items():
                if k < len(cells):
                    c_off, cell = cells[k]
                    value = _clean_value(cell)
                    if _plausible(kind, value, strict=True) or (kind not in ("PERSON", "ADDRESS", "EMAIL")
                                                                 and _plausible(kind, value, strict=False)
                                                                 and any(ch.isalnum() for ch in value)):
                        spans.append(_span_in(c_off, cell, value, 0, kind))
    return spans


def _pattern_spans(text: str) -> list[Span]:
    spans: list[Span] = []
    for m in EMAIL_RE.finditer(text):      # above vault values, so a name inside an address stays one e-mail
        spans.append(Span(m.start(), m.end(), "EMAIL", m.group(), 5))
    for m in IBAN_RE.finditer(text):       # the longest start of the match that passes the checksum: a match may
        cand = m.group()                   # run on into the next word ("... 2180 today")
        for end in range(len(cand), 14, -1):
            piece = cand[:end]
            if piece[-1].isalnum() and iban_ok(piece):
                spans.append(Span(m.start(), m.start() + end, "IBAN", piece, 2))
                break
    for m in CARD_RE.finditer(text):
        value = m.group().rstrip(" -.")
        if luhn_ok(value):
            spans.append(Span(m.start(), m.start() + len(value), "CARD", value, 2))
    for rx in PHONE_RES:
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


def _context_spans(text: str) -> list[Span]:
    """Numbers, dates and addresses named in a sentence ("my passport number is N12345678")."""
    spans: list[Span] = []
    for m in CODE_CONTEXT_RE.finditer(text):
        code, noun = m.group("code"), m.group("noun").lower()
        letters, digits = sum(c.isalpha() for c in code), sum(c.isdigit() for c in code)
        if len(code) < 5 and not (letters and digits and len(code) >= 4):
            continue
        if re.fullmatch(r"(19|20)\d{2}", code) or re.fullmatch(_DATE, code, flags=re.IGNORECASE):
            continue
        kind = ("NATIONAL_ID" if noun in ("passport", "iqama", "national id", "id") else "SERIAL" if noun == "serial"
                else "VIN" if noun in ("chassis", "vin") else "ACCOUNT")
        spans.append(Span(m.start("code"), m.end("code"), kind, code, 2))
    for m in PLATE_CONTEXT_RE.finditer(text):
        spans.append(Span(m.start("code"), m.end("code"), "PLATE", m.group("code"), 2))
    for m in BIRTH_CONTEXT_RE.finditer(text):
        spans.append(Span(m.start("date"), m.end("date"), "BIRTHDATE", m.group("date"), 2))
    for rx in (STREET_RE, SHORT_ADDRESS_RE):
        for m in rx.finditer(text):
            spans.append(Span(m.start(), m.end(), "ADDRESS", m.group().rstrip(" ,"), 2))
    return spans


def _flexible(value: str) -> str:
    """A pattern for a number written with any grouping: '608010167519' also finds '6080 1016 7519'."""
    return r"[\s\-./]?".join(re.escape(c) for c in _alnum(value))


def _literal_spans(text: str, literals: list[tuple[str, str, bool, str]]) -> list[Span]:
    """Every occurrence of known values: whole words; name parts case-sensitive; numbers in any grouping."""
    spans: list[Span] = []
    det = text.translate(ARABIC_DIGITS)
    for value, kind, part, placeholder in literals:
        if len(value) < 3 or _too_short(kind, value):
            continue
        if kind in ALNUM_KINDS + NUMERIC_KINDS and len(_alnum(value)) >= 6:
            pattern, flags = _flexible(value), re.IGNORECASE
            if kind == "PHONE":
                pattern = r"(?:\+|\()?" + pattern + r"\)?"
        else:
            pattern = re.escape(value).replace(r"\ ", r"\s+")
            flags = 0 if part else re.IGNORECASE
        for m in re.finditer(rf"(?<![\w]){pattern}(?![\w])", det, flags=flags):
            spans.append(Span(m.start(), m.end(), kind, value, 4, placeholder))
    return spans


def _resolve(spans: list[Span]) -> list[Span]:
    """Non-overlapping spans: higher priority first, then longer."""
    chosen: list[Span] = []
    for s in sorted(spans, key=lambda s: (-s.priority, -(s.end - s.start), s.start)):
        if s.end > s.start and all(s.end <= c.start or s.start >= c.end for c in chosen):
            chosen.append(s)
    return sorted(chosen, key=lambda s: s.start)


def _digit_runs(text: str) -> list[str]:
    """Digits of each number in the text, with spaces, dashes and dots inside one number allowed."""
    return [_digits(m.group()) for m in re.finditer(r"\d(?:[ \-.]?\d)*", text.translate(ARABIC_DIGITS))]


def _alnum_runs(text: str) -> list[str]:
    return [_alnum(m.group()) for m in re.finditer(r"[A-Za-z0-9](?:[\s\-./]?[A-Za-z0-9])*",
                                                    text.translate(ARABIC_DIGITS))]


@dataclass
class VaultEntry:
    placeholder: str
    kind: str
    value: str
    aliases: list[str] = field(default_factory=list)     # other spellings of the value (any case)
    source: str = ""
    parts: list[str] = field(default_factory=list)       # a person's first or last name alone (case-sensitive)

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
        keyed = kind in NUMERIC_KINDS + ALNUM_KINDS
        key = _number_key(kind, value) if keyed else " ".join(value.lower().split())
        for e in self.entries.values():
            if keyed != (e.kind in NUMERIC_KINDS + ALNUM_KINDS) or (keyed and e.kind != kind):
                continue
            for s in e.strings():
                other = _number_key(e.kind, s) if keyed else " ".join(s.lower().split())
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
            if kind == "PERSON":
                self._add_parts(self.entries[placeholder])
            return placeholder

    def _add_parts(self, entry: VaultEntry) -> None:
        """First and last names of a person, so "Mr. Haddad" is hidden after "Layla Haddad" was."""
        for word in re.split(r"[\s,]+", entry.value):
            word = word.strip(".'’")
            if (len(word) >= 3 and word.lower() not in _NAME_PARTICLES and word not in entry.parts
                    and word not in entry.aliases and (word[0].isupper() or "؀" <= word[0] <= "ۿ")):
                entry.parts.append(word)

    def lookup(self, kind: str, value: str) -> str | None:
        """The placeholder of a value already in the vault, without adding anything."""
        with self._lock:
            found = self._find(kind, value)
            return found.placeholder if found else None

    def add_known_person(self, full_name: str) -> str:
        """A person the user names in their profile: the full name and each part map to one placeholder, in any
        case (the user may type their own name in lower case)."""
        full_name = " ".join(full_name.split())
        with self._lock:
            placeholder = self.placeholder_for("PERSON", full_name, source="your profile")
            entry = self.entries[placeholder]
            for part in full_name.split():
                if len(part) >= 3 and part not in entry.strings():
                    entry.aliases.append(part)
            entry.parts = [p for p in entry.parts if p not in entry.aliases]
            if full_name not in self.known_people:
                self.known_people.append(full_name)
            return placeholder

    def _literals(self) -> list[tuple[str, str, bool, str]]:
        return ([(s, e.kind, False, e.placeholder) for e in self.entries.values() for s in e.strings()]
                + [(p, e.kind, True, e.placeholder) for e in self.entries.values() for p in e.parts])

    def _spans(self, text: str) -> list[Span]:
        det = text.translate(ARABIC_DIGITS)          # same length, so offsets carry over to the original
        keep = [Span(m.start(), m.end(), _KEEP, m.group(), 9) for m in _STRICT_PLACEHOLDER_RE.finditer(text)]
        found = _label_spans(det) + _pattern_spans(det) + _context_spans(det)
        return _resolve(keep + _literal_spans(text, self._literals()) + found)

    def redact(self, text: str, source: str = "") -> tuple[str, list[VaultEntry]]:
        """Replace every personal detail with its placeholder. Returns the safe text and the entries used."""
        with self._lock:
            for s in self._spans(text):                # pass 1: register every value found
                if s.kind != _KEEP and s.priority != 4:
                    self.placeholder_for(s.kind, text[s.start:s.end].strip(), source)
            out: list[str] = []
            used: dict[str, VaultEntry] = {}
            cursor = 0
            for s in self._spans(text):                # pass 2: replace every occurrence of every known value
                out.append(text[cursor:s.start])
                if s.kind == _KEEP:
                    out.append(s.value)
                else:
                    placeholder = s.placeholder or self.placeholder_for(s.kind, text[s.start:s.end].strip(), source)
                    out.append(placeholder)
                    used[placeholder] = self.entries[placeholder]
                cursor = s.end
            out.append(text[cursor:])
            return "".join(out), list(used.values())

    def mask_known(self, text: str) -> str:
        """Replace only values already in the vault (for web results and tool output: public text is not
        scanned for new details, so a company's support number is not hidden)."""
        with self._lock:
            spans = _resolve(_literal_spans(text, self._literals()))
            out, cursor = [], 0
            for s in spans:
                out.append(text[cursor:s.start])
                out.append(s.placeholder or self.placeholder_for(s.kind, s.value))
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
        squashed = " ".join(payload.split())
        lowered = squashed.lower()
        digit_runs, alnum_runs = _digit_runs(payload), _alnum_runs(payload)
        for e in self.entries.values():
            hit = False
            for s in e.strings():
                if len(s) < 3 or _too_short(e.kind, s):
                    continue
                key = _number_key(e.kind, s)
                if e.kind in NUMERIC_KINDS and len(key) >= 7:
                    hit = any(key in run for run in digit_runs)
                elif e.kind in ALNUM_KINDS and len(key) >= 5:
                    hit = any(key in run for run in alnum_runs)
                else:
                    hit = re.search(rf"(?<!\w){re.escape(' '.join(s.lower().split()))}(?!\w)", lowered) is not None
                if hit:
                    break
            if not hit:
                hit = any(re.search(rf"(?<!\w){re.escape(p)}(?!\w)", squashed) for p in e.parts if len(p) >= 3)
            if hit:
                found.append(e.placeholder)
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
