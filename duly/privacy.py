"""Privacy firewall: personal details become placeholders before any text leaves this computer.

Detection is plain code that runs locally, so no model ever sees the raw text:

- patterns, with checksums where one exists: IBANs (mod 97, and the exact length the IBAN registry sets for the
  country), payment cards (Luhn), e-mail addresses, phone numbers (international, 00 and 966 prefixes, Saudi mobiles
  and landlines, US and other national formats), Saudi national ID and iqama numbers (10 digits starting with 1 or
  2), vehicle VINs;
- labelled fields in English and Arabic: the value after "Name:", "Policy No.:", "D.O.B.", "Address" ... on the same
  line (also without a colon, as text copied from PDF forms reads, if the value looks like its kind), several fields
  on one line, values on the lines below a label that stands alone, two-column rows, the columns of tables whose
  header row holds labels, and the names in e-mail headers (From, To, Cc);
- numbers and dates named in sentences: "my passport number is ...", "policy MTR-...", "born on ...", street
  addresses with a house number, P.O. boxes, Saudi short addresses;
- the people the user names in their profile, and every value already in the vault wherever it appears again,
  including the first or last name alone of any person found (as written or in capitals), and account numbers
  grouped differently.

Arabic-Indic digits are read as digits. Dates, amounts and company names are kept: the assistant needs them, so
headings ("Customer Contact Details"), dates in number runs and sentences after an address label are not taken for
personal details.

Redaction runs in two passes. The first registers every value the detectors find, in full, even where it contains a
value already in the vault ("Omar Nasser" when "Nasser" is known, an IBAN that holds a known account number). The
second replaces every occurrence of every known value, the longest first, so a detail repeated later in the same text
is hidden too. The vault maps each placeholder to its value, lives in the user's data folder, and puts the real values
back only on the user's screen. Before every outbound call, a leak check scans the whole payload for any value in the
vault and blocks the call if one is found. What the detectors miss cannot be blocked: a name with no label that is not
in the profile is the main example.
"""

from __future__ import annotations

import bisect
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

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
DIGIT_KINDS = NUMERIC_KINDS + ALNUM_KINDS + ("BIRTHDATE",)     # values that always hold a digit
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

# Roles that forms follow with "name": "Driver name", "Father's name".
_NAME_ROLES = ("driver", "student", "patient", "landlord", "tenant", "first", "last", "middle", "given", "family",
               "father", "mother", "spouse", "wife", "husband", "guardian", "parent", "child", "employee",
               "beneficiary", "applicant", "cardholder", "passenger", "guest", "contact", "nominee", "witness",
               "sponsor")
_FAMILY_ROLES = ("father", "mother", "spouse", "wife", "husband", "guardian", "parent", "child")
EMAIL_HEADERS = ("from", "to", "cc", "bcc")            # names only, and only with a colon: "To renew, ..." is prose
ONE_WORD_LABELS = {"first name", "last name", "middle name", "given name", "family name", "surname"}

# Labels whose value is personal, matched case-insensitively at the start of a line or table cell.
LABELS: dict[str, tuple[str, ...]] = {
    "PERSON": ("full name", "name", "customer", "customer name", "policyholder", "policy holder", "insured name",
               "insured", "insured person", "name of insured", "tenant", "lessee", "landlord", "lessor",
               "member name", "account holder", "account name", "subscriber", "beneficiary", "driver",
               "named driver", "owner", "client", "member", "buyer", "sold to", "bill to", "billed to", "ship to",
               "deliver to", "emergency contact", "cardholder", "card holder", "patient", "student", "employee",
               "applicant", "guest", "passenger", "spouse", "dependant", "dependent", "next of kin", "attention",
               "attn", "signed by", "surname", "guardian", "nominee", "witness", "sponsor",
               *(f"{role} name" for role in _NAME_ROLES), *(f"{role}'s name" for role in _FAMILY_ROLES),
               *EMAIL_HEADERS, "الاسم", "اسم العميل", "اسم المؤمن له", "حامل الوثيقة", "المستأجر", "المؤجر",
               "اسم المستأجر", "صاحب الحساب"),
    "NATIONAL_ID": ("national id", "national id number", "id number", "iqama", "iqama number", "passport",
                    "passport number", "civil id", "residence permit", "رقم الهوية", "الهوية الوطنية",
                    "رقم الإقامة", "رقم الاقامة", "رقم الجواز", "رقم جواز السفر", "رقم السجل المدني"),
    "BIRTHDATE": ("date of birth", "dob", "birth date", "birthday", "تاريخ الميلاد"),
    "PHONE": ("phone", "mobile", "tel", "telephone", "phone number", "mobile number", "contact number", "fax",
              "landline", "home phone", "work phone", "رقم الجوال", "الجوال", "رقم الهاتف", "الهاتف"),
    "EMAIL": ("email", "e-mail", "email address", "البريد الإلكتروني", "البريد الالكتروني"),
    "ADDRESS": ("address", "home address", "billing address", "property address", "premises", "unit address",
                "delivery address", "shipping address", "national address", "residential address",
                "mailing address", "postal address", "permanent address", "current address", "po box", "street",
                "street name", "district", "postal code", "postcode", "zip code", "short address", "العنوان",
                "العنوان الوطني", "عنوان السكن"),
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
# Nouns that name a number when followed by "No.", "Nr.", "Number" or "#": "Policy No.: ...", "Building No. 4127".
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
    **{n: "ADDRESS" for n in ("building", "additional", "unit", "apartment", "flat")},   # Saudi National Address
}
_LABEL_TO_KIND = {label: kind for kind, labels in LABELS.items() for label in labels}
_ARABIC_RE = re.compile(r"[؀-ۿ]")

# Words of headings and form captions: a candidate name holding one is not a person ("Customer Contact Details",
# "Dear Policy Holder", the header cell "Relation"). Plurals are matched too.
HEADING_WORDS = frozenset("""
details detail information info contact vehicle policy holder policyholder address number date schedule agreement
contract terms conditions summary section page total amount payment insurance coverage cover premium account
statement invoice receipt form signature name data record report notice document service center centre department
team office branch card member customer client owner driver tenant landlord valued sir madam nationality saudi
arabia kingdom relation relationship spouse wife husband son daughter father mother child children parent guardian
brother sister family dependant dependent gender status type plan period phone mobile email telephone fax identity
id passport iqama birth place country region district building apartment floor unit street road avenue boulevard
highway tower complex compound plaza residence villa city airport terminal station code reference serial
plate chassis model item product fee tax vat balance renewal expiry validity valid due end issue issued start
benefit claim deductible excess limit insured beneficiary applicant employee employer company manager support care
desk helpdesk subject regards sincerely emergency kin male female marital religion occupation profession rent
deposit lease property premises monthly annual yearly obligation responsibility responsibilities instruction
declaration handbook password portal login package assistance party parties both proof purchase warranty guarantee
resident patient student guest passenger cardholder homeowner neighbour neighbor doctor teacher primary secondary
additional named self single married divorced widowed title category class level tier version translation arabic
description note remarks comment attachment attached appendix annex general important reminder confirmation request
application registration membership subscription subscriber cancellation termination refund return delivery
shipping order booking reservation ticket all user colleague friend services sales billing claims hiring recipient
whom partner see above below same none nil unknown n/a na applicable not dear hello thank thanks you your our my the
and or of for with to from in on by at this that new mr mrs ms miss dr eng prof
""".split())
# Lower-case words that make a value a sentence, not an address: "Address changes must be notified within 30 days".
_SENTENCE_WORDS = frozenset("""
must be is are was were will shall should can could may might would been being has have had does do did not please
if when within after before unless until which that this these those you your we our they their it its he she his
her them us notify notified notice change changes changed update updated send sent provide provided required apply
applies see
""".split())
# Words that end a name after a name label: "Sami Nasser and family", "Layla Haddad of Riyadh".
_STOP_WORDS = frozenset("and or the of for with from to at in on by as is are was were family others children kids "
                        "etc".split())
_ROLE_NOTES = frozenset("policyholder tenant member owner customer insured landlord".split())
_TITLES = frozenset("mr mrs ms miss dr eng prof".split())
_NAME_PARTICLES = frozenset("al el bin bint ibn abu abd ben bou de da del della di du dos van von der den la le "
                            "y".split())
_NOT_PARTS = _NAME_PARTICLES | _TITLES | {"sir", "the", "and", "of"}   # never a name part to look for alone

# The IBAN registry: each country's IBAN length. A run of letters and digits is an IBAN only with one of these
# country codes and exactly this length, so "on 06 November 2026" never is, whatever its checksum.
IBAN_LENGTHS: dict[str, int] = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22, "BH": 22, "BI": 27, "BR": 29,
    "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DJ": 27, "DK": 18, "DO": 28, "EE": 20, "EG": 29,
    "ES": 24, "FI": 18, "FK": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28,
    "HN": 28, "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20,
    "LB": 28, "LC": 32, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "LY": 25, "MC": 27, "MD": 24, "ME": 22, "MK": 19,
    "MN": 20, "MR": 27, "MT": 31, "MU": 30, "NI": 28, "NL": 18, "NO": 15, "OM": 23, "PK": 24, "PL": 28, "PS": 29,
    "PT": 25, "QA": 29, "RO": 24, "RS": 22, "RU": 33, "SA": 24, "SC": 31, "SD": 18, "SE": 24, "SI": 19, "SK": 24,
    "SM": 27, "SO": 23, "ST": 25, "SV": 28, "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20,
    "YE": 30,
}


def _phrase_re(phrase: str) -> str:
    if phrase == "dob":
        return r"d\.?[ \t]?o\.?[ \t]?b\.?"
    if phrase == "po box":
        return r"p\.?[ \t]?o\.?[ \t]?box"
    words = [re.escape(w).replace("'", "['’]") for w in phrase.split()]
    return r"\.?[ \t]+".join(words) + r"\.?"


_PHRASES = sorted(set(_LABEL_TO_KIND) | set(NUMBER_NOUNS), key=len, reverse=True)
_PHRASE_ALT = "|".join(f"(?:{_phrase_re(p)})" for p in _PHRASES)
_SUFFIX = r"[ \t]*+(?:no|nr|num|number)\b\.?|[ \t]*+#"
# A label at the start of a line or cell, its "No." suffix, a parenthesis, a separator, and the value. Possessive
# quantifiers: a long run of spaces is read once, never backtracked into (that took minutes on indented pages).
_LINE_RE = re.compile(
    rf"^(?P<pre>[ \t>*\-•]*+)(?P<label>{_PHRASE_ALT})(?![A-Za-z؀-ۿ])(?P<suffix>{_SUFFIX})?+"
    rf"(?P<paren>[ \t]*+\([^)\n]*\))?+(?P<sep>[ \t]*+\**+[ \t]*+[:：=][ \t]*+\**+[ \t]*+|[ \t]++)?+(?P<value>.*)",
    re.IGNORECASE)
# Where the value of a field ends and the next "Label:" begins: a tab, two or more spaces before a short caption and
# a colon, or one space before a known label, a single word or an Arabic word and a colon ("Name: Layla Haddad
# Nationality: Saudi").
_NEXT_LABEL_RE = re.compile(
    r"\t"
    r"|(?<![ ])[ ]{2,}+(?=[^\s:：]{1,30}(?:[ ][^\s:：]{1,30}){0,3}[ ]?[:：])"
    rf"|[ ](?=(?:{_PHRASE_ALT})(?![A-Za-z؀-ۿ])(?:{_SUFFIX})?+[ \t]*+[:：])"
    r"|[ ](?=[A-Za-z][A-Za-z'’\-]*+\.?[ ]?[:：])"
    r"|[ ](?=[؀-ۿ]++[ ]?[:：])",
    re.IGNORECASE)

EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
# (pattern, fewest digits, most digits)
PHONE_RES: list[tuple[re.Pattern[str], int, int]] = [
    (re.compile(r"(?<![\w+])\+\d{1,3}(?:[ \-.]?\(?\d{1,4}\)?){2,6}(?!\w)"), 8, 15),          # +966 50 555 0134
    (re.compile(r"(?<![\d+])00\d{1,3}(?:[ \-.]?\(?\d{1,4}\)?){2,6}(?!\d)"), 8, 15),          # 00966 50 ..., 0044 20
    (re.compile(r"(?<![\d+])966[ \-.]?(?:5\d|1[1-7])(?:[ \-.]?\d){7}(?!\d)"), 8, 15),       # 966501234567
    (re.compile(r"(?<![\d+])\(?05\d\)?(?:[ \-.]?\d){7}(?!\d)"), 8, 15),                     # 050 555 0134
    (re.compile(r"(?<![\d+])\(?01[1-7]\)?(?:[ \-.]?\d){7}(?!\d)"), 8, 15),                  # 013 860 1234 (landline)
    (re.compile(r"(?<![\d+.])5\d[ \-.]\d{3}[ \-.]\d{4}(?!\d)"), 8, 15),                     # 50 123 4567
    (re.compile(r"(?<![\d+])\(\d{3}\)[ \-.]?\d{3}[ \-.]\d{4}(?!\d)"), 8, 15),               # (415) 555-0123
    (re.compile(r"(?<![\w+\-./])\d{3}([\-.])\d{3}\1\d{4}(?![\w\-]|[./]\d)"), 10, 10),        # 415-555-0123
    # A national number with a leading 0 written in groups: 020 7946 0958 (UK), 010 1234 5678 (Egypt). The
    # separators are required and the same throughout, so "05-11-2026 10:30" is not one.
    (re.compile(r"(?<![\w+\-./])\(?0\d{1,4}\)?([ \-])\d{2,4}(?:\1\d{2,4}){1,2}(?![\w\-]|[./]\d)"), 10, 11),
]
IBAN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2}[ ]?\d{2}(?:[ \-]?[A-Za-z0-9]{4}){2,7}(?:[ \-]?[A-Za-z0-9]{1,4})?"
                     r"(?![A-Za-z0-9])")
# 13 to 19 digits in one run, or printed in groups of four (4-4-4-4, 4-4-4-4-3), or Amex 4-6-5 and Diners 4-6-4:
# never a run of dates such as "2026-11-29 2027-11-28".
CARD_RE = re.compile(r"(?<![\d\-])(?<!\d[./])(?:\d{13,19}|\d{4}(?P<sep>[ \-.])\d{4}(?P=sep)\d{4}(?P=sep)\d{4}"
                     r"(?:(?P=sep)\d{3})?|\d{4}[ \-]\d{6}[ \-]\d{4,5})(?![\d\-]|[./]\d)")
NATIONAL_ID_RE = re.compile(r"(?<![\d+\-/])(?<!\d\.)[12]\d{9}(?![\d\-/]|\.\d)")
VIN_RE = re.compile(r"(?<![A-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9])")
DEAR_RE = re.compile(r"\bDear[ \t]++(?:(?:Mr|Mrs|Ms|Miss|Dr|Eng|Prof)\.?[ \t]++)?+"
                     r"(?P<name>[^\W\d_][\w'’\-]*+(?:[ \t]++[^\W\d_][\w'’\-]*+){0,3})")
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
          r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_DATE = (rf"\d{{1,2}}[/.\-]\d{{1,2}}[/.\-]\d{{2,4}}|\d{{4}}[/.\-]\d{{1,2}}[/.\-]\d{{1,2}}|"
         rf"\d{{1,2}}(?:st|nd|rd|th)?[ \t]+(?:of[ \t]+)?{_MONTH},?[ \t]+\d{{4}}|{_MONTH}[ \t]+\d{{1,2}}(?:st|nd|rd|th)?,?[ \t]+\d{{4}}")
# Numbers and dates named in a sentence.
CODE_CONTEXT_RE = re.compile(
    r"\b(?P<noun>passport|policy|account|acct|contract|membership|member|iqama|national id|id|reference|ref|serial|"
    r"chassis|vin|invoice|claim|booking|order|customer|subscription|ticket|certificate|lease|ejar)\b"
    r"(?:[ \t]++(?:number|no\.?|num\.?|nr\.?)|[ \t]*+#)?+(?:[ \t]++(?:is|was|=)|[ \t]*+[:=])?+[ \t]*+"
    r"(?P<code>(?=[A-Za-z0-9\-/]*\d)[A-Za-z0-9][A-Za-z0-9\-/]{2,}[A-Za-z0-9])", re.IGNORECASE)
# A plate in a sentence: letters in capitals, so "renew the plate by 30 June" is not one.
PLATE_CONTEXT_RE = re.compile(
    r"(?i:\bplate\b)(?:[ \t]++(?i:number|no\.?))?+(?:[ \t]++(?i:is|was))?+[ \t]*+[:#]?[ \t]*+"
    r"(?P<code>[A-Z]{1,3}[ \t]?\d{1,4}|\d{1,4}[ \t]?[A-Z]{1,3})\b")
BIRTH_CONTEXT_RE = re.compile(
    rf"\b(?:born|date[ \t]+of[ \t]+birth|birth[ \t]?date|birthday|d\.?o\.?b\.?)(?:[ \t]++(?:on|in|is|was))?+"
    rf"[ \t]*+:?[ \t]*+(?P<date>{_DATE})", re.IGNORECASE)
_STREET_WORD = r"(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Lane|Ln|Drive|Dr|Way|Highway|Hwy|Square|Sq)"
_PLACE_TAIL = r"(?:,[ \t]*+(?:[A-Z][\w'’\-]*+[ \t]?){1,4})*(?:[ \t]*+\d{5})?"
# A house number, at least one capitalised name, a street word: not "120 Sq m" or "4 Wheel Drive".
STREET_RE = re.compile(
    r"\b\d{1,5}[A-Za-z]?[ \t]++(?:(?!(?:Wheel|Hard|Flash|Test|Disk|Thumb|Pen|USB|SSD|HDD|Optical|State)\b)"
    rf"[A-Z][\w'’\-]*+[ \t]++){{1,4}}{_STREET_WORD}\b\.?(?![ \t]*(?:m|ft|km|metres?|meters?|feet|mi)\b|[ \t]*m²)"
    rf"{_PLACE_TAIL}")
# A P.O. box, with the city and postal code after it ("P.O. Box 12345 Riyadh 11564") or a comma and the city.
PO_BOX_RE = re.compile(r"\b(?i:p\.?[ \t]?o\.?[ \t]?box)[ \t]*+(?:(?i:no)\.?[ \t]*+)?\d{1,7}\b"
                       rf"(?:,?[ \t]++(?:[A-Z][\w'’\-]*+,?[ \t]++){{1,3}}\d{{5}}\b|{_PLACE_TAIL})")
SHORT_ADDRESS_RE = re.compile(r"\b[A-Z]{4}\d{4}\b")          # Saudi National Address short code
# What an address written without a colon must show: a house or building number before a capitalised word, a
# street word, a postal code, a P.O. box, or a building, district, apartment or unit word.
_ADDRESS_SHAPE_RE = re.compile(
    rf"\d{{1,6}}[A-Za-z]?,?[ \t]+[A-Z]|\b{_STREET_WORD}\b|(?<!\d)\d{{5}}(?!\d)|(?i:\bp\.?[ \t]?o\.?[ \t]?box\b)"
    r"|(?i:\b(?:building|bldg|district|apartment|apt|flat|unit|villa|floor|block|suite)\b)")

PLACEHOLDER_RE = re.compile(rf"\[?\b({'|'.join(KINDS)})[_ ](\d+)\b\]?")      # tolerant, for restoring
_STRICT_PLACEHOLDER_RE = re.compile(rf"\[({'|'.join(KINDS)})_(\d+)\]")       # what redaction leaves alone
# A value under a name label that is a company, not a person: kept, because companies are not personal data
# and the assistant needs them (whom to write to, whose procedure to look up).
COMPANY_RE = re.compile(r"\b(co|company|corp|corporation|inc|llc|ltd|limited|est|establishment|group|holding|bank|"
                        r"insurance|mutual|real estate|properties|trading|motors|electronics|club|fitness|telecom|"
                        r"services|agency|hospital|clinic|school|university|college|academy|institute|foundation|"
                        r"ministry|authority|municipality|council|partners|associates|solutions|systems|"
                        r"technologies|industries|international|enterprises|airlines|airways|pharmacy|hotel|"
                        r"supermarket|market|mall|store|shop|شركة|مؤسسة|مجموعة|بنك|مصرف|مستشفى|مدرسة|جامعة|"
                        r"للتأمين|العقارية)\b\.?", re.IGNORECASE)
_PARTICLE_PREFIX_RE = re.compile(r"(?:al|el|bin|bint|ibn|abu|abd|ben|bou)[-‐'’](?=[^\W\d_])", re.IGNORECASE)
_ARABIC_NAME_RE = re.compile(r"^[؀-ۿ]+(?:[ \t]+[؀-ۿ]+){1,4}$")
_LAST_FIRST_RE = re.compile(r"(?:(?i:al|el|bin|ibn|abu|abd|de|da|del|di|du|van|von|la|le)[ \t]+)?[^\W\d_][\w'’\-]*+,"
                            r"[ \t]*+[^\W\d_][\w'’\-]*+(?:[ \t]+[^\W\d_][\w'’\-]*+)?")     # "Al Haddad, Layla"
_TRAILING = " \t.,;:)]}\"'*"
_KEEP = "KEEP"


class PrivacyError(RuntimeError):
    """A payload about to leave this computer still contains a value from the vault."""


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text.translate(ARABIC_DIGITS))


def _alnum(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.translate(ARABIC_DIGITS).lower())


def _text_key(text: str) -> str:
    return " ".join(text.lower().split())


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
    """A registered country code, that country's exact IBAN length, and the mod-97 checksum."""
    s = re.sub(r"[\s\-]", "", candidate).upper()
    if len(s) != IBAN_LENGTHS.get(s[:2], 0) or not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", s):
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


def _iban_in(candidate: str) -> str | None:
    """The IBAN at the start of a match, cut at its country's length: a match may run on into the next word."""
    need, count = IBAN_LENGTHS.get(candidate[:2].upper(), 0), 0
    for i, c in enumerate(candidate):
        count += c.isalnum()
        if count == need:
            piece = candidate[:i + 1]
            return piece if iban_ok(piece) else None
    return None


@dataclass
class Span:
    start: int
    end: int
    kind: str
    value: str
    priority: int   # placeholders > e-mails > vault values > labelled fields > checksummed patterns > others
    placeholder: str | None = None      # for vault values: the entry they belong to


class LabelHit(NamedTuple):
    kind: str
    mode: str          # colon, header (From:, To:), number (a "No." suffix, no colon), form (whitespace only), alone
    match: re.Match[str]
    label: str


def _norm_label(text: str) -> str:
    norm = " ".join(re.sub(r"\.", " ", text.lower().replace("’", "'")).split())
    if re.fullmatch(r"d ?o ?b", norm):
        return "dob"
    return "po box" if re.fullmatch(r"p ?o ?box", norm) else norm


def _heading_word(word: str) -> bool:
    w = re.sub(r"['’]s$", "", word.lower().strip(".,;:'’\"()"))
    return w in HEADING_WORDS or (w.endswith("s") and w[:-1] in HEADING_WORDS) or (
        w.endswith("es") and w[:-2] in HEADING_WORDS)


def _all_heading(value: str) -> bool:
    """Only caption words: "Policy Holder", "Same as above", "N/A"."""
    words = re.findall(r"[^\W\d_]+(?:/[^\W\d_]+)?", value)
    return bool(words) and all(_heading_word(w) for w in words)


def _sentence(text: str) -> bool:
    return bool({w.lower() for w in re.findall(r"[^\W\d_]+", text)} & _SENTENCE_WORDS)


def _is_name_word(word: str) -> bool:
    """A capitalised word of letters (any alphabet), an initial, a particle ("al", "bin", "de") or a particle joined
    to a name ("al-Haddad")."""
    word = word.rstrip(",")
    if word.lower() in _NAME_PARTICLES:
        return True
    m = _PARTICLE_PREFIX_RE.match(word)
    core = word[m.end():] if m else word
    if re.fullmatch(r"[^\W\d_]\.?", core):
        return core[0].isupper()
    return len(core) >= 2 and core[0].isupper() and all(c.isalpha() or c in "'’-" for c in core)


def _name_like(value: str, one_word: bool = False) -> bool:
    """Two to five name words (one where the label asks for one), after an optional title, none of them a heading
    word; or Arabic words."""
    if len(value) > 60:
        return False
    if _ARABIC_NAME_RE.match(value) or (one_word and re.fullmatch(r"[؀-ۿ]+", value)):
        return True
    words = value.split()
    if words and words[0].rstrip(".").lower() in _TITLES:
        words = words[1:]
    if not (2 <= len(words) <= 5 or (one_word and len(words) == 1)):
        return False
    if not all(_is_name_word(w) for w in words) or all(w.lower() in _NAME_PARTICLES for w in words):
        return False
    return not any(_heading_word(w) for w in words)


def _address_like(value: str) -> bool:
    return len(value) <= 160 and not _sentence(value) and bool(_ADDRESS_SHAPE_RE.search(value))


def _drop_note(value: str, any_note: bool) -> str:
    """A note in brackets after a value is not part of it: any short note after a number ("0501234567 (home)"),
    a role after anything else ("Building 4127 (tenant)")."""
    if not value.endswith(")") or "(" not in value:
        return value
    i = value.rindex("(")
    note = value[i + 1:-1]
    if i == 0 or (len(note) > 40 if any_note else note.strip().lower() not in _ROLE_NOTES):
        return value
    return value[:i].rstrip()


def _person_value(value: str) -> str:
    """The name in a value after a name label, without what follows it: "Sami Nasser and family", "Layla Haddad,
    4512 King Fahd Road", "Omar Nasser 2123456789", "Layla Haddad (wife)" each give the name alone."""
    if COMPANY_RE.search(value):
        return value                                    # a company: _plausible rejects it whole
    if value.endswith(")") and "(" in value:
        i = value.rindex("(")
        note = re.findall(r"[^\W\d_]+", value[i + 1:-1])
        if i > 0 and not any(w[0].isupper() and not _heading_word(w) for w in note):
            value = value[:i].rstrip()
    if not _LAST_FIRST_RE.fullmatch(value):
        value = re.split(r"[,;]", value, maxsplit=1)[0]
    words = list(re.finditer(r"\S+", value))
    for k, w in enumerate(words):
        word = w.group()
        follows = words[k + 1].group() if k + 1 < len(words) else ""
        joined = word.lower() in ("and", "&") and _is_name_word(follows)       # "Layla and Omar Haddad"
        if any(c.isdigit() for c in word) or word.startswith("(") or (word.lower() in _STOP_WORDS and not joined):
            return value[:w.start()].rstrip()
    return value


def _clean_value(value: str, kind: str) -> str:
    value = _NEXT_LABEL_RE.split(value, maxsplit=1)[0].rstrip(_TRAILING.replace(")", ""))
    value = _person_value(value) if kind == "PERSON" else _drop_note(value, any_note=kind in DIGIT_KINDS)
    return value.strip(_TRAILING)


def _too_short(kind: str, value: str) -> bool:
    """A number too short to identify anyone ("Reference: 2026"): hiding it would hide every date with 2026 in it."""
    if kind not in NUMERIC_KINDS + ALNUM_KINDS:
        return False
    key = _alnum(value)
    return len(key) < 5 and not (len(key) >= 4 and re.search(r"[a-z]", key) and re.search(r"\d", key))


def _literal_ok(kind: str, value: str) -> bool:
    """A known value worth finding everywhere: not too short, and not a bare number under five digits ("Building No:
    2026"), which would hide years and amounts in every later text. Redaction and the leak check agree on this."""
    return (len(value) >= 3 and not _too_short(kind, value)
            and not re.fullmatch(r"\d{1,4}", value.strip().translate(ARABIC_DIGITS)))


def _plausible(kind: str, value: str, strict: bool, one_word: bool = False, numbered: bool = False) -> bool:
    """A value worth hiding. Strict applies where there is no colon (PDF forms, the line below a label, table
    columns): there it must look like its kind, so a heading such as "Address changes" is left alone."""
    if len(value) < 2 or _STRICT_PLACEHOLDER_RE.fullmatch(value) or not any(c.isalnum() for c in value):
        return False                                  # also a blank form line: "Signed by: ________"
    if _too_short(kind, value):
        return False
    has_digit = any(c.isdigit() for c in value.translate(ARABIC_DIGITS))
    if kind in DIGIT_KINDS and not has_digit:          # "Ref: Renewal" names no number
        return False
    if kind == "EMAIL" and "@" not in value:
        return False
    if kind in ("PERSON", "ADDRESS") and _all_heading(value):     # "Name: Policy Holder", "Address: see below"
        return False
    if kind == "PERSON" and COMPANY_RE.search(value):
        return False
    if not strict:
        return True
    if ":" in value or "：" in value:                 # another label follows: this is not a value
        return False
    if kind == "PERSON":
        return _name_like(value, one_word)
    if kind == "ADDRESS":                             # "Building No. 4127" is a number; "Address 4512 King ..." a place
        return _address_like(value) or (numbered and has_digit and len(value) <= 12 and len(value.split()) <= 2)
    if kind == "EMAIL":
        return True
    return len(value) <= 40 and len(value.split()) <= (8 if kind in NUMERIC_KINDS else 4)


def _value(m: re.Match[str]) -> str:
    return m.group("value").rstrip(" \t*")


def _label_match(cell: str) -> LabelHit | None:
    """The label a cell starts with, its kind and how its value follows it."""
    m = _LINE_RE.match(cell)
    if not m:
        return None
    label, suffix = _norm_label(m.group("label")), bool(m.group("suffix"))
    sep, value = m.group("sep") or "", _value(m)
    if suffix and label in NUMBER_NOUNS:
        kind = NUMBER_NOUNS[label]
    elif label in _LABEL_TO_KIND:
        kind = _LABEL_TO_KIND[label]
    else:
        return None
    colon = bool(re.search(r"[:：]", sep))
    if label in EMAIL_HEADERS and not colon:
        return None                                  # "To renew ...", "From 17 December": only "To:" is a label
    if not value.strip():                            # (an Arabic label alone without a colon counts only as a
        return LabelHit(kind, "alone", m, label)     # table cell, never with the lines below it)
    if _ARABIC_RE.search(label) and not colon:
        return None                                  # Arabic prose: "المستأجر ملزم ..." starts with a label word
    if re.search(r"[:：=]", sep):
        return LabelHit(kind, "header" if label in EMAIL_HEADERS else "colon", m, label)
    if suffix:
        return LabelHit(kind, "number", m, label)
    if sep:
        return LabelHit(kind, "form", m, label)
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


def _value_spans(offset: int, text: str, hit: LabelHit | None) -> list[Span]:
    """The value of one "Label: value" field."""
    if hit is None or hit.mode == "alone":
        return []
    m = hit.match
    if hit.mode == "header":                  # "From: Layla Haddad <layla@example.com>; Omar Nasser": names only
        spans = []
        for part in re.finditer(r"[^,;]+", _value(m)):
            name = _clean_value(part.group().split("<", 1)[0], "PERSON").strip("\"“”")
            if _plausible("PERSON", name, strict=True):
                spans.append(_span_in(offset, text, name, m.start("value") + part.start(), "PERSON"))
        return spans
    value = _clean_value(_value(m), hit.kind)
    if _plausible(hit.kind, value, strict=hit.mode != "colon", one_word=hit.label in ONE_WORD_LABELS,
                  numbered=hit.mode == "number"):
        return [_span_in(offset, text, value, m.start("value"), hit.kind)]
    return []


def _field_spans(offset: int, cell: str) -> list[Span]:
    """The values of the labelled fields in a cell; one line may hold several ("Name: Layla Haddad Sponsor: ...")."""
    spans: list[Span] = []
    pos = 0
    while pos < len(cell):
        hit = _label_match(cell[pos:])
        value_at = pos + (hit.match.start("value") if hit is not None and hit.mode != "alone" else 0)
        nxt = _NEXT_LABEL_RE.search(cell, value_at + 1)
        end = nxt.start() if nxt else len(cell)
        if hit is not None and hit.mode != "alone":
            spans.extend(_value_spans(offset + pos, cell[pos:end], _label_match(cell[pos:end])))
        if nxt is None:
            break
        pos = nxt.end()
    return spans


def _header_row(lines: list[tuple[int, str]], li: int) -> bool:
    """A table header: the next line is the |---|---| separator."""
    nxt = lines[li + 1][1] if li + 1 < len(lines) else ""
    return "-" in nxt and set(nxt.strip()) <= set("|-: ")


def _label_spans(text: str) -> list[Span]:
    spans: list[Span] = []
    lines = _lines(text)
    for li, (offset, line) in enumerate(lines):
        cells = _cells(offset, line)
        filled = [i for i, (_, cell) in enumerate(cells) if cell.strip()]
        for i, (c_off, cell) in enumerate(cells):
            spans.extend(_field_spans(c_off, cell))
            hit = _label_match(cell)
            if hit is None or hit.mode != "alone":
                continue
            if len(filled) == 2 and filled[0] == i:    # a two-column row: | Name | Sami Nasser |
                # In a table's header row ("| Member | Relation |") the second cell must look like its kind.
                n_off, nxt = cells[filled[1]]
                value = _clean_value(nxt, hit.kind)
                strict = hit.kind != "PERSON" or _header_row(lines, li)
                if _label_match(nxt) is None and _plausible(hit.kind, value, strict=strict):
                    spans.append(_span_in(n_off, nxt, value, 0, hit.kind))
            elif len(cells) == 1 and (re.search(r"[:：]", hit.match.group("sep") or "")
                                      or not _ARABIC_RE.search(hit.label)):
                spans.extend(_below(lines, li, hit.kind, hit.label))   # the label stands alone: its value is below
    spans.extend(_table_column_spans(lines))
    return spans


_ADDRESSEE_LABELS = {"shipping address", "billing address", "delivery address", "mailing address", "postal address"}


def _below(lines: list[tuple[int, str]], li: int, kind: str, label: str = "") -> list[Span]:
    """Values on the lines under a label that stands alone ("Bill To:" then a name and an address)."""
    block: list[tuple[int, str]] = []
    for offset, line in lines[li + 1: li + 6]:
        if not line.strip() or _label_match(line) is not None or re.search(r"[:：]\s*\S", line):
            break
        block.append((offset, line))
    if not block:
        return []
    spans: list[Span] = []

    def address(rows: list[tuple[int, str]]) -> bool:
        """The lines from the top that read as an address, as one span; a sentence or a heading ends it."""
        taken = []
        for row in rows:
            if _sentence(row[1]) or _all_heading(row[1]):
                break
            taken.append(row)
        if not taken or not _address_like(", ".join(r[1].strip() for r in taken)):
            return False
        first_off, first = taken[0]
        last_off, last = taken[-1]
        start = first_off + len(first) - len(first.lstrip())
        value_end = last_off + len(last.rstrip(_TRAILING + " "))
        if value_end > start:
            spans.append(Span(start, value_end, "ADDRESS", "", 3))
        return True

    first_off, first = block[0]
    name = _clean_value(first, "PERSON")
    if kind == "PERSON":
        guess = (_plausible("PERSON", name, strict=False) and not any(c.isdigit() for c in name)
                 and len(name.split()) <= 5 and not any(_heading_word(w) for w in name.split()))
        if _plausible("PERSON", name, strict=True) or guess:
            spans.append(_span_in(first_off, first, name, 0, "PERSON"))
            address(block[1:4])
    elif kind == "ADDRESS":       # a shipping or billing address often starts with whom it is for
        if label in _ADDRESSEE_LABELS and _plausible("PERSON", name, strict=True) and address(block[1:5]):
            spans.append(_span_in(first_off, first, name, 0, "PERSON"))
        else:
            address(block[:4])
    else:
        value = _clean_value(first, kind)
        if _plausible(kind, value, strict=True):
            spans.append(_span_in(first_off, first, value, 0, kind))
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
        columns: dict[int, LabelHit] = {}
        for k, (_, cell) in enumerate(header):
            hit = _label_match(cell)
            if hit is not None and hit.mode == "alone":
                columns[k] = hit
        if not columns:
            continue
        for row_off, row in block[1:]:
            cells = _cells(row_off, row)
            if all(set(c.strip()) <= set("-: ") for _, c in cells):
                continue                                   # the |---|---| separator row
            for k, hit in columns.items():
                if k < len(cells):
                    c_off, cell = cells[k]
                    value = _clean_value(cell, hit.kind)
                    if (_plausible(hit.kind, value, strict=True, one_word=hit.label in ONE_WORD_LABELS)
                            or (hit.kind not in ("PERSON", "ADDRESS", "EMAIL")
                                and _plausible(hit.kind, value, strict=False) and any(ch.isalnum() for ch in value))):
                        spans.append(_span_in(c_off, cell, value, 0, hit.kind))
    return spans


def _dear_spans(text: str) -> list[Span]:
    """The name after "Dear": not "Dear Dell Support", "Dear Policy Holder" or "Dear Sir or Madam"."""
    spans: list[Span] = []
    for m in DEAR_RE.finditer(text):
        run = []
        for w in re.finditer(r"\S+", m.group("name")):
            if not _is_name_word(w.group()):
                break
            run.append(w)
        while run and run[-1].group().lower() in _NAME_PARTICLES:
            run.pop()
        if not run or any(_heading_word(w.group()) for w in run):
            continue
        name = m.group("name")[:run[-1].end()]
        if not COMPANY_RE.search(name):
            spans.append(Span(m.start("name"), m.start("name") + len(name), "PERSON", name, 1))
    return spans


def _pattern_spans(text: str) -> list[Span]:
    spans: list[Span] = []
    for m in EMAIL_RE.finditer(text):      # above vault values, so a name inside an address stays one e-mail
        spans.append(Span(m.start(), m.end(), "EMAIL", m.group(), 5))
    for m in IBAN_RE.finditer(text):
        piece = _iban_in(m.group())
        if piece:
            spans.append(Span(m.start(), m.start() + len(piece), "IBAN", piece, 2))
    for m in CARD_RE.finditer(text):
        value = m.group().rstrip(" -.")
        groups = re.split(r"[ \-.]", value)
        if luhn_ok(value) and not (len(groups) > 1 and all(re.fullmatch(r"(?:19|20)\d\d", g) for g in groups)):
            spans.append(Span(m.start(), m.start() + len(value), "CARD", value, 2))
    for rx, fewest, most in PHONE_RES:
        for m in rx.finditer(text):
            value = m.group().rstrip(" -.")
            if fewest <= len(_digits(value)) <= most:
                spans.append(Span(m.start(), m.start() + len(value), "PHONE", value, 1))
    for m in NATIONAL_ID_RE.finditer(text):
        spans.append(Span(m.start(), m.end(), "NATIONAL_ID", m.group(), 1))
    for m in VIN_RE.finditer(text):
        v = m.group()
        if sum(c.isdigit() for c in v) >= 2 and sum(c.isalpha() for c in v) >= 2:
            spans.append(Span(m.start(), m.end(), "VIN", v, 1))
    return spans + _dear_spans(text)


def _context_spans(text: str) -> list[Span]:
    """Numbers, dates and addresses named in a sentence ("my passport number is N12345678")."""
    spans: list[Span] = []
    for m in CODE_CONTEXT_RE.finditer(text):
        code, noun = m.group("code"), m.group("noun").lower()
        letters, digits = sum(c.isalpha() for c in code), sum(c.isdigit() for c in code)
        if len(code) < 5 and not (letters and digits and len(code) >= 4):
            continue
        if re.fullmatch(r"(?:19|20)\d\d(?:[-/](?:19|20)?\d{1,2})?", code) or re.fullmatch(_DATE, code, re.IGNORECASE):
            continue                       # a year, a year range or month ("policy 2026-2027"), a date
        kind = ("NATIONAL_ID" if noun in ("passport", "iqama", "national id", "id") else "SERIAL" if noun == "serial"
                else "VIN" if noun in ("chassis", "vin") else "ACCOUNT")
        spans.append(Span(m.start("code"), m.end("code"), kind, code, 2))
    for m in PLATE_CONTEXT_RE.finditer(text):
        spans.append(Span(m.start("code"), m.end("code"), "PLATE", m.group("code"), 2))
    for m in BIRTH_CONTEXT_RE.finditer(text):
        spans.append(Span(m.start("date"), m.end("date"), "BIRTHDATE", m.group("date"), 2))
    for rx in (STREET_RE, PO_BOX_RE, SHORT_ADDRESS_RE):
        for m in rx.finditer(text):
            value = m.group().rstrip(" ,")
            spans.append(Span(m.start(), m.start() + len(value), "ADDRESS", value, 2))
    return spans


def _flexible(value: str) -> str:
    """A pattern for a number written with any grouping: '608010167519' also finds '6080 1016 7519'."""
    return r"[\s\-./]?".join(re.escape(c) for c in _alnum(value))


def _part_forms(part: str) -> list[str]:
    """A name part as written and in capitals; a part found in capitals also as a capitalised word."""
    forms = {part, part.upper()}
    if part.isupper() or part.islower():
        forms.add(part[:1].upper() + part[1:].lower())
    return sorted(forms, key=len, reverse=True)


def _literal_spans(text: str, literals: list[tuple[str, str, bool, str]]) -> list[Span]:
    """Every occurrence of known values: whole words; name parts as written or in capitals; numbers in any
    grouping."""
    spans: list[Span] = []
    det = text.translate(ARABIC_DIGITS)
    squashed, letters_digits = _text_key(det), _alnum(det)      # a value absent from these is skipped unsearched
    for value, kind, part, placeholder in literals:
        if not _literal_ok(kind, value):
            continue
        if kind in ALNUM_KINDS + NUMERIC_KINDS and len(_alnum(value)) >= 6:
            if _alnum(value) not in letters_digits:
                continue
            pattern, flags = _flexible(value), re.IGNORECASE
            if kind == "PHONE":
                pattern = r"(?:\+|\()?" + pattern + r"\)?"
        elif part:
            forms = _part_forms(value)
            if not any(f in det for f in forms):
                continue
            pattern, flags = "|".join(re.escape(f) for f in forms), 0
        else:
            words = value.translate(ARABIC_DIGITS).split()
            if _text_key(" ".join(words)) not in squashed:
                continue
            pattern, flags = r"\s+".join(re.escape(w) for w in words), re.IGNORECASE
        for m in re.finditer(rf"(?<![\w])(?:{pattern})(?![\w])", det, flags=flags):
            spans.append(Span(m.start(), m.end(), kind, value, 4, placeholder))
    return spans


def _resolve(spans: list[Span]) -> list[Span]:
    """Non-overlapping spans: higher priority first, then longer. The chosen spans are kept in order of their start,
    so each candidate is checked against its two neighbours only (a long table has thousands of spans)."""
    chosen: list[Span] = []
    starts: list[int] = []
    for s in sorted(spans, key=lambda s: (-s.priority, -(s.end - s.start), s.start)):
        i = bisect.bisect_right(starts, s.start)
        if s.end > s.start and (i == 0 or chosen[i - 1].end <= s.start) and (i == len(chosen)
                                                                              or chosen[i].start >= s.end):
            chosen.insert(i, s)
            starts.insert(i, s.start)
    return chosen


def _digit_runs(text: str) -> list[str]:
    """Digits of each number in the text, with spaces, dashes and dots inside one number allowed."""
    return [_digits(m.group()) for m in re.finditer(r"\d(?:[ \-.]?\d)*", text.translate(ARABIC_DIGITS))]


def _alnum_runs(text: str) -> list[str]:
    return [_alnum(m.group()) for m in re.finditer(r"[A-Za-z0-9](?:[\s\-./]?[A-Za-z0-9])*",
                                                    text.translate(ARABIC_DIGITS))]


def _flatten(payload: Any) -> str:
    """Every string in a payload, one per line. json.dumps would write a line break as the two characters '\\n',
    which puts a letter before the first word of every line and hides a name there from the whole-word check."""
    out: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                walk(k)
                walk(v)
        elif isinstance(x, (list, tuple, set)):
            for v in x:
                walk(v)
        elif isinstance(x, str):
            out.append(x)
            if x.lstrip()[:1] in ("{", "["):           # tool-call arguments are JSON inside a string
                try:
                    inner = json.loads(x)
                except ValueError:
                    return
                if isinstance(inner, (dict, list)):
                    walk(inner)
        elif x is not None:
            out.append(str(x))

    walk(payload)
    return "\n".join(out)


@dataclass
class VaultEntry:
    placeholder: str
    kind: str
    value: str
    aliases: list[str] = field(default_factory=list)     # other spellings of the value (any case)
    source: str = ""
    parts: list[str] = field(default_factory=list)       # a person's first or last name alone (as written or capitals)

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
        key = _number_key(kind, value) if keyed else _text_key(value)
        for e in self.entries.values():
            if keyed != (e.kind in NUMERIC_KINDS + ALNUM_KINDS) or (keyed and e.kind != kind):
                continue
            for s in e.strings():
                other = _number_key(e.kind, s) if keyed else _text_key(s)
                if other and other == key:
                    return e
        if kind == "PERSON" and len(value.split()) == 1:     # "Dear Haddad," after "Khalid Haddad"
            return next((e for e in self.entries.values() if e.kind == "PERSON" and _is_part(e, value)), None)
        if kind == "ADDRESS":         # the same place written more fully: "..., Riyadh 13325, Apartment 6"
            for e in self.entries.values():
                if e.kind == "ADDRESS" and any(_extends(key, _text_key(s)) for s in e.strings() if _address_like(s)):
                    return e
        return None

    def placeholder_for(self, kind: str, value: str, source: str = "") -> str:
        with self._lock:
            found = self._find(kind, value)
            if found is not None:
                if value not in found.strings() and not _is_part(found, value):
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
        """First and last names of a person, so "Mr. Haddad" is hidden after "Layla Haddad" was. Arabic names get
        none: Arabic joins prefixes to words, and a first name alone is too common to hide everywhere."""
        if _ARABIC_RE.search(entry.value):
            return
        for word in re.split(r"[\s,]+", entry.value):
            word = word.strip(".'’()")
            m = _PARTICLE_PREFIX_RE.match(word)
            word = word[m.end():] if m else word            # "al-Haddad" -> "Haddad"
            if (len(word) >= 3 and word.lower() not in _NOT_PARTS and not _heading_word(word)
                    and word not in entry.parts and word not in entry.aliases and word[0].isupper()):
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

    def redact(self, text: str, source: str = "") -> tuple[str, list[VaultEntry]]:
        """Replace every personal detail with its placeholder. Returns the safe text and the entries used."""
        with self._lock:
            det = text.translate(ARABIC_DIGITS)          # same length, so offsets carry over to the original
            keep = [Span(m.start(), m.end(), _KEEP, m.group(), 9) for m in _STRICT_PLACEHOLDER_RE.finditer(text)]
            found = _label_spans(det) + _pattern_spans(det) + _context_spans(det)
            # Pass 1: register what the detectors find, in full. Known values are left out of this choice, so
            # "Nasser" from the profile does not cut "Omar Nasser" short; only a number that holds a known number
            # of its own kind with words around it ("HMI-MTR-2291-4471 renewed") keeps the known one.
            known: dict[str, list[tuple[int, int]]] = {}
            for k in _literal_spans(text, self._literals()):
                known.setdefault(k.kind, []).append((k.start, k.end))
            for ranges in known.values():
                ranges.sort()
            this_text: dict[str, list[tuple[str, str]]] = {}     # (value, letters and digits) found earlier here
            for s in _resolve(keep + found):
                if s.kind == _KEEP:
                    continue
                value = text[s.start:s.end].strip()
                if s.kind not in ("PERSON", "ADDRESS"):
                    key = _alnum(value)
                    earlier = [(v, s.kind, False, "") for v, k in this_text.get(s.kind, []) if k in key]
                    if _holds(known.get(s.kind, []), s) or (earlier and _literal_spans(value, earlier)):
                        continue
                    this_text.setdefault(s.kind, []).append((value, key))
                self.placeholder_for(s.kind, value, source)
            # Pass 2: replace every occurrence of every known value, the longest first.
            out: list[str] = []
            used: dict[str, VaultEntry] = {}
            cursor = 0
            for s in _resolve(keep + _literal_spans(text, self._literals()) + found):
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
        squashed = " ".join(payload.translate(ARABIC_DIGITS).split())
        lowered = squashed.lower()
        digit_runs, alnum_runs = _digit_runs(payload), _alnum_runs(payload)
        for e in self.entries.values():
            hit = False
            for s in e.strings():
                if not _literal_ok(e.kind, s):
                    continue
                key = _number_key(e.kind, s)
                if e.kind in NUMERIC_KINDS and len(key) >= 7:
                    hit = any(key in run for run in digit_runs)
                elif e.kind in ALNUM_KINDS and len(key) >= 5:
                    hit = any(key in run for run in alnum_runs)
                else:
                    key = _text_key(s.translate(ARABIC_DIGITS))
                    hit = re.search(rf"(?<!\w){re.escape(key)}(?!\w)", lowered) is not None
                if hit:
                    break
            if not hit:
                hit = any(re.search(rf"(?<!\w)(?:{'|'.join(re.escape(f) for f in _part_forms(p))})(?!\w)", squashed)
                          for p in e.parts if len(p) >= 3)
            if hit:
                found.append(e.placeholder)
        return found

    def check_outbound(self, payload: Any, destination: str) -> None:
        """Raise PrivacyError if anything about to go to `destination` contains a vault value. A payload already
        written as JSON (a list of chat messages) is read back first, so its strings are checked as written."""
        if isinstance(payload, str):
            try:
                parsed = json.loads(payload) if payload.lstrip()[:1] in ("{", "[") else None
            except ValueError:
                parsed = None
            text = _flatten(parsed) if isinstance(parsed, (dict, list)) else payload
        else:
            text = _flatten(payload)
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


def _holds(ranges: list[tuple[int, int]], s: Span) -> bool:
    """One of the sorted (start, end) ranges lies inside the span."""
    i = bisect.bisect_left(ranges, (s.start, -1))
    while i < len(ranges) and ranges[i][0] < s.end:
        if ranges[i][1] <= s.end:
            return True
        i += 1
    return False


def _is_part(entry: VaultEntry, value: str) -> bool:
    """A single word that is one of a person's name parts, in any case."""
    word = value.strip(".'’").lower()
    return entry.kind == "PERSON" and " " not in value.strip() and any(p.lower() == word for p in entry.parts)


def _extends(longer: str, shorter: str) -> bool:
    """`longer` is `shorter` with more after it: the same address with a unit or district added."""
    return len(longer) > len(shorter) >= 10 and longer.startswith(shorter) and not longer[len(shorter)].isalnum()


def count_by_kind(entries: list[VaultEntry]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.kind] = counts.get(e.kind, 0) + 1
    return counts
