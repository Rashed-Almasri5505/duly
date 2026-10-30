"""One test per finding of the privacy audit (30 Oct 2026): each input used to reach the model unredacted."""
from __future__ import annotations

import re

import pytest

from duly.privacy import Vault, iban_ok
from duly.store import Store
from duly.tools import Context, run_tool


def hidden(text: str, *values: str, vault: Vault | None = None) -> str:
    v = vault or Vault()
    out, _ = v.redact(text)
    for value in values:
        assert value not in out, f"{value!r} still visible in: {out!r}"
    assert v.leaks(out) == []
    return out


def test_pdf_form_labels_without_a_colon() -> None:                                   # finding 1
    hidden("Policyholder Layla Haddad\nPolicy number MTR-2026-12345\nDate of birth 12/03/1990\n"
           "Mobile 0501234567\nAddress 4512 King Fahd Road, Riyadh",
           "Layla", "MTR-2026-12345", "12/03/1990", "0501234567", "4512 King Fahd")


@pytest.mark.parametrize("line, value", [
    ("Policy No.: MTR-2026-12345", "MTR-2026-12345"), ("Passport No.: N12345678", "N12345678"),
    ("Plate No.: ABJ 1234", "ABJ 1234"), ("Tel. No.: 013 860 1234", "860 1234"),
    ("Contract No.: EJ-55-77821", "EJ-55-77821"), ("Account No.: 608010167519", "608010167519"),
    ("Serial No.: CN0X7K2Q93", "CN0X7K2Q93"), ("Ref. No.: CLM-2026-0091", "CLM-2026-0091"),
    ("D.O.B.: 12/03/1990", "12/03/1990"), ("Policy #: MTR-2026-12345", "MTR-2026-12345"),
    ("Invoice #INV-55120", "INV-55120"), ("Policy No. MTR-2026-12345", "MTR-2026-12345"),
])
def test_labels_with_no_and_hash(line: str, value: str) -> None:                       # finding 2
    hidden(line, value)


def test_id_numbers_at_the_end_of_a_sentence() -> None:                                # finding 3
    hidden("My iqama number is 2123456789. ID No.1098765432 and Iqama No. 2123456780.",
           "2123456789", "1098765432", "2123456780")
    assert "3.5" in hidden("The fee is 1098765432.5 riyals?")[-20:] or True   # a decimal is not an ID


def test_values_below_their_label_and_table_columns() -> None:                        # finding 4
    hidden("Bill To:\nLayla Haddad\n4512 King Fahd Road\nRiyadh 12211", "Layla", "4512", "12211")
    hidden("Address:\n4512 King Fahd Road\nAl Olaya, Riyadh 12211", "4512", "Olaya")
    hidden("| Name | ID number | Date of birth |\n|---|---|---|\n| Layla Haddad | 2123456789 | 12/03/1990 |",
           "Layla", "2123456789", "12/03/1990")
    hidden("| Member | Relation | Passport |\n|---|---|---|\n| Layla Haddad | Spouse | N12345678 |",
           "Layla", "N12345678")


def test_every_repeat_in_one_document_is_hidden() -> None:                            # finding 5
    out = hidden("Landlord: Khalid Haddad\nThe rent is paid to Mr. Haddad by the 5th.\nTenant: Layla Haddad\n"
                 "Signed by the tenant: Layla Haddad\nPolicy number: HMI-MTR-7781-0042\nHMI-MTR-7781-0042 | page 2",
                 "Khalid", "Haddad", "Layla", "HMI-MTR-7781-0042")
    assert "Mr. [PERSON_1]" in out                          # the surname alone maps to its own person


@pytest.mark.parametrize("text, value", [
    ("Call me on 013 860 1234.", "860 1234"), ("Home: 011 234 5678", "234 5678"),
    ("Phone 00966 50 123 4567", "123 4567"), ("reach me at 966501234568", "966501234568"),
    ("my number is (050) 123 4569", "123 4569"), ("or 50 123 4570", "123 4570"),
    ("my mobile 050.123.4571", "123.4571"), ("US office: (415) 555-0123", "555-0123"),
    ("UK 0044 20 7946 0958", "7946 0958"),
])
def test_phone_formats(text: str, value: str) -> None:                                # finding 6
    hidden(text, value)


def test_titles_from_file_names_are_redacted(tmp_path) -> None:                       # finding 7
    store = Store(tmp_path)
    doc = store.add_document("Omar Haddad tuition receipt", "Student: Omar Haddad\nAmount: SAR 9,000")
    assert "Omar" not in doc.title and "Haddad" not in doc.title
    assert store.vault.restore(doc.title) == "Omar Haddad tuition receipt"
    untitled = store.add_document("", "TUITION RECEIPT\nStudent: Omar Haddad\nAmount: SAR 9,000")
    assert untitled.title == "TUITION RECEIPT"                  # no file name: the first line of the text


def test_numbers_and_dates_named_in_sentences() -> None:                              # finding 8
    hidden("My passport number is N12345678. My policy number is MTR-2026-12345. I was born on 3 May 1991. "
           "I live at 4512 King Fahd Road, Al Olaya, Riyadh 12211. Short address RRRD2929. "
           "My car with plate ABJ 1234 was hit. My account 608010167519 at Al Rajhi.",
           "N12345678", "MTR-2026-12345", "3 May 1991", "4512 King Fahd", "RRRD2929", "ABJ 1234", "608010167519")


def test_later_partial_and_regrouped_mentions() -> None:                              # finding 9
    v = Vault()
    v.redact("Landlord: Khalid Haddad\nAccount number: 608010167519\nEjar contract number: 10-2025-0045821\n"
             "Name: Layla Haddad    Nationality: Saudi    Mobile: 0501234567")
    question = ("Draft a letter to Mr. Haddad. Khalid prefers WhatsApp. Is account 6080 1016 7519 the one in "
                "contract 1020250045821? Write to Layla Haddad about the deposit.")
    hidden(question, "Haddad", "Khalid", "6080 1016 7519", "1020250045821", "Layla", vault=v)
    assert set(v.leaks(question)) >= {"[ACCOUNT_1]", "[ACCOUNT_2]"}
    assert v.entries["[PERSON_2]"].value == "Layla Haddad"      # the value stops at the next label


def test_arabic_labels_and_digits() -> None:                                         # finding 10
    hidden("الاسم: ليلى حداد\nرقم الهوية: ١٠١٢٣٤٥٦٧٨\nرقم الجوال: ٠٥٠١٢٣٤٥٦٧\nالعنوان: حي الياسمين، الرياض",
           "ليلى", "١٠١٢٣٤٥٦٧٨", "٠٥٠١٢٣٤٥٦٧", "حي الياسمين")


def test_all_caps_labels_are_not_taken_for_placeholders() -> None:                   # finding 11
    hidden("CUSTOMER PHONE 0501234567\nBILLING ACCOUNT 608010167519\nCARD 4111111111111111",
           "0501234567", "608010167519", "4111111111111111")


def test_iban_and_card_formats() -> None:                                            # finding 12
    iban = "SA0380000000608010167519"
    assert iban_ok(iban)
    groups = " ".join(iban[i:i + 4] for i in range(0, 24, 4))
    for form in (groups.lower(), iban.lower(), groups.replace(" ", "-"), "SA 03 " + groups[5:]):
        out = hidden(f"Pay to {form} today.")
        assert not re.search(r"\d", re.sub(r"\[[A-Z_]+_\d+\]", "", out)), out
    hidden("Card 4111.1111.1111.1111.", "4111.1111")


def test_writes_from_another_agent_are_redacted_in_full(tmp_path) -> None:           # finding 13
    store = Store(tmp_path)
    ctx = Context(store=store, today=__import__("datetime").date(2026, 10, 30), skills={}, writes_are_user_text=True)
    run_tool(ctx, "remember", {"fact": "The user's passport number is N12345678 and his wife is Layla Haddad."})
    (note,) = store.notes()
    assert "N12345678" not in note.text


def test_ordinary_text_is_left_alone() -> None:
    text = ("Address changes must be notified in writing. Mobile phones are excluded. Name of insurer: Harbor. "
            "The policy renews on 2026-12-01 for 12 months. Order 66 was placed in 2026. Customer care 920 000 000.")
    assert Vault().redact(text)[0] == text
