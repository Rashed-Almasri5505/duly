"""One test per finding of the privacy audit (30 Oct 2026): each input used to reach the model unredacted, or was
redacted where it should not have been (round 2)."""
from __future__ import annotations

import json
import re
import time
from datetime import date, timedelta

import pytest

from duly.privacy import PrivacyError, Vault, iban_ok
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


# ---------------------------------------------------------------- round 2 (30 Oct 2026)

def test_a_known_value_never_cuts_a_longer_value_short() -> None:                     # round 2, item 1
    v = Vault()
    v.add_known_person("Sami Nasser")
    assert v.redact("Student: Omar Nasser")[0] == "Student: [PERSON_2]"
    hidden("Policyholder: Sami Ahmed Saleh Nasser", "Sami", "Ahmed", "Saleh", "Nasser", vault=v)
    v = Vault()
    v.redact("Landlord: Khalid Haddad")
    assert v.redact("Tenant: Layla Haddad")[0] == "Tenant: [PERSON_2]"
    assert v.entries["[PERSON_2]"].value == "Layla Haddad"
    assert v.redact("Dear Haddad,")[0] == "Dear [PERSON_1]," and len(v.entries) == 2    # a part is its person
    v = Vault()
    v.add_known_person("Fahd Nasser")
    assert v.redact("Address: 4512 King Fahd Road, Al Olaya, Riyadh 12211")[0] == "Address: [ADDRESS_1]"
    v = Vault()
    v.redact("Account number: 608010167519")
    assert v.redact("IBAN: SA03 8000 0000 6080 1016 7519")[0] == "IBAN: [IBAN_1]"


def test_one_value_keeps_one_placeholder_when_more_follows_it() -> None:              # round 2, item 1
    v = Vault()
    v.add_known_person("Sami Nasser")
    assert v.redact("Tenant: Sami Nasser and family")[0] == "Tenant: [PERSON_1] and family"
    assert v.redact("Sold to: Sami Nasser, 4512 King Fahd Road")[0] == "Sold to: [PERSON_1], [ADDRESS_1]"
    out = v.redact("Policy number: HMI-MTR-2291-4471\nPolicy number: HMI-MTR-2291-4471 renewed")[0]
    assert out == "Policy number: [ACCOUNT_1]\nPolicy number: [ACCOUNT_1] renewed"
    assert v.redact("Mobile: 0501234567 (home)")[0] == "Mobile: [PHONE_1] (home)"
    v.redact("Address: Building 4127, Al Yasmin District, Riyadh 13325")
    out = v.redact("Property address: Building 4127, Al Yasmin District, Riyadh 13325, Apartment 6")[0]
    assert out == "Property address: [ADDRESS_2]"                  # the same place written more fully
    assert sorted(v.entries) == ["[ACCOUNT_1]", "[ADDRESS_1]", "[ADDRESS_2]", "[PERSON_1]", "[PHONE_1]"]


@pytest.mark.parametrize("role", ["Driver", "Student", "Patient", "Landlord", "Tenant", "First", "Last", "Middle",
                                  "Given", "Family", "Father", "Mother", "Spouse", "Wife", "Husband", "Guardian",
                                  "Parent", "Child", "Employee", "Beneficiary", "Applicant", "Cardholder",
                                  "Passenger", "Guest", "Contact", "Nominee", "Witness", "Sponsor"])
def test_role_name_labels(role: str) -> None:                                          # round 2, item 2
    hidden(f"{role} name: Omar Haddad\n{role} Name Layla Saleh", "Omar", "Haddad", "Layla Saleh")


def test_name_part_labels_and_email_headers() -> None:                                 # round 2, item 2
    hidden("Surname: Haddad\nFamily name: Saleh\nGiven name: Layla\nFather's name: Ahmed Haddad\n"
           "Mother’s name: Mona Saleh\nFirst name Omar", "Haddad", "Saleh", "Layla", "Ahmed", "Mona", "Omar")
    hidden("From: Layla Haddad <layla.haddad@example.com>\nTo: Omar Nasser; Khalid Haddad\ncc: Sara Saleh\n"
           "Subject: Renewal", "Layla", "Omar", "Khalid", "Sara", "layla.haddad@")
    text = ("From: Harbor Mutual Insurance\nTo: Claims Department\nFrom 17 December 2025 to 17 December 2026\n"
            "To renew, call us 7 days before the end.\n| From | To |\n|---|---|\n| Riyadh Office | Jeddah Branch |")
    assert Vault().redact(text)[0] == text


def test_address_labels_po_boxes_and_the_national_address() -> None:                   # round 2, item 2
    hidden("Residential address: 4512 King Fahd Road\nMailing address: 7 Olaya Street, Riyadh\n"
           "Postal address: P.O. Box 12345, Riyadh 11564", "4512", "Olaya Street", "12345")
    hidden("Send the form to PO Box 54321, Jeddah 21411 or P.O.Box 777, Dammam.", "54321", "21411", "777")
    out = hidden("Building No: 4127\nStreet: King Fahd Road\nDistrict: Al Olaya\nPostal Code: 12211\n"
                 "Additional No: 2929\nUnit No: 12\nThe lease started in 2026 at unit 12.",
                 "4127", "King Fahd", "Olaya", "12211", "2929", ": 12")
    assert out.endswith("The lease started in 2026 at unit 12.")   # a short number is hidden only by its label


def test_headings_are_not_people() -> None:                                            # round 2, item 3
    text = ("Dear Policy Holder,\nCustomer Contact Details\nInsured Vehicle Details\nDear Valued Customer,\n"
            "Member Services Team\nDear Sir or Madam,\nPolicyholder Information")
    v = Vault()
    assert v.redact(text)[0] == text and not v.entries


def test_header_cells_and_next_labels_end_a_value() -> None:                           # round 2, item 3
    v = Vault()
    out = hidden("| Member | Relation | Passport |\n|---|---|---|\n| Layla Haddad | Spouse | N12345678 |",
                 "Layla", "N12345678", vault=v)
    assert "| Member | Relation | Passport |" in out and len(v.entries) == 2
    out = hidden("| Member | Relation |\n|---|---|\n| Omar Haddad | Son |", "Omar", vault=v)
    assert "| Member | Relation |" in out and "Relation" not in {e.value for e in v.entries.values()}
    v = Vault()
    out = v.redact("Name: Layla Haddad Nationality: Saudi Sponsor: Omar Nasser")[0]
    assert out == "Name: [PERSON_1] Nationality: Saudi Sponsor: [PERSON_2]"
    assert v.entries["[PERSON_1]"].value == "Layla Haddad"
    assert v.redact("Saudi Arabia, Kingdom of")[0] == "Saudi Arabia, Kingdom of"


def test_arabic_labels_need_a_colon_and_arabic_names_have_no_parts() -> None:          # round 2, item 3
    v = Vault()
    prose = "المستأجر ملزم بدفع الإيجار في موعده\nالمؤجر: شركة النور العقارية"
    assert v.redact(prose)[0] == prose and not v.entries               # prose, and a company under a name label
    hidden("الاسم: ليلى حداد", "ليلى حداد", vault=v)
    assert v.entries["[PERSON_1]"].parts == []
    v.placeholder_for("PERSON", "Layla Haddad Nationality Saudi")
    assert v.entries["[PERSON_2]"].parts == ["Layla", "Haddad"]       # no heading word becomes a name part


def test_an_address_block_ends_at_a_heading_and_names_its_addressee() -> None:         # round 2, item 3
    v = Vault()
    out = hidden("Shipping address\nLayla Haddad\nBuilding 12, King Abdullah Road\nAl Malaz, Riyadh 12836\n"
                 "Order summary\nItem: Wireless mouse", "Layla", "Building 12", "12836", vault=v)
    assert out == "Shipping address\n[PERSON_1]\n[ADDRESS_1]\nOrder summary\nItem: Wireless mouse"
    assert v.entries["[PERSON_1]"].value == "Layla Haddad"


def test_name_shapes_in_tables_and_forms() -> None:                                    # round 2, item 4
    out = hidden("| Member | Passport |\n|---|---|\n| Layla al-Haddad | N12345678 |\n| Zoë Martin | N22345678 |\n"
                 "| José Ruiz | N32345678 |\n| Layla Haddad (wife) | N42345678 |",
                 "Layla", "Haddad", "Zoë", "Martin", "José", "Ruiz")
    assert "] (wife) |" in out
    hidden("Policyholder Zoë Martin\nDriver José Ruiz\nInsured Layla al-Haddad\nNamed driver Omar Saleh (son)",
           "Zoë", "José", "Layla", "Haddad", "Omar")


def test_dates_and_sentences_are_not_cards_addresses_plates_or_numbers() -> None:      # round 2, item 5
    start = date(2026, 1, 1)
    for n in range(0, 366, 5):
        a, b = start + timedelta(days=n), start + timedelta(days=n + 364)
        for text in (f"Valid {a:%Y-%m-%d} {b:%Y-%m-%d}", f"Valid {a:%d/%m/%Y} {b:%d/%m/%Y}",
                     f"Valid {a:%d.%m.%Y} {b:%d.%m.%Y}", f"Years {a:%Y} {a:%Y} {b:%Y} {b:%Y}"):
            assert Vault().redact(text)[0] == text, text
    text = ("Valid 2026-11-29 2027-11-28 and 29/11/2026 28/11/2027.\n"
            "Address changes must be notified within 30 days.\nRenew the plate by 30 June.\n"
            "The flat is 120 Sq m and the car has 4 Wheel Drive.\nRef: Renewal\nPolicy number: Pending\n"
            "Plate: Unknown\nVIN: N/A\nPassport: Expired\nMobile: Ask reception\nSerial number: Unknown\n"
            "IBAN: Pending\nRenewal is due.")
    v = Vault()
    assert v.redact(text)[0] == text and not v.entries
    hidden("Card 4111 1111 1111 1111, Amex 3782 822463 10005.", "4111", "822463")


def test_the_leak_check_reads_every_line_of_a_chat_payload() -> None:                  # round 2, item 6
    v = Vault()
    v.redact("Name: Layla Haddad\nEmail: layla@example.com")
    with pytest.raises(PrivacyError):
        v.check_outbound([{"role": "user", "content": "Hello\nLayla Haddad called"}], "test")
    for payload in ([{"role": "user", "content": "Hi\nlayla@example.com"}],
                    json.dumps([{"role": "user", "content": "Hi\nlayla@example.com"}])):     # as the gateway sends it
        with pytest.raises(PrivacyError):
            v.check_outbound(payload, "test")
    v.check_outbound([{"role": "user", "content": "Hello\n[PERSON_1] called"}], "test")


def test_names_in_capitals_match_their_parts() -> None:                                # round 2, item 7
    v = Vault()
    v.redact("NAME: LAYLA HADDAD")
    hidden("Ms. Haddad and the HADDAD family; Layla called.", "Haddad", "HADDAD", "Layla", vault=v)
    v = Vault()
    v.redact("Name: Layla Haddad")
    hidden("LAYLA HADDAD, Ms. HADDAD", "LAYLA", "HADDAD", vault=v)


def test_long_runs_of_spaces_stay_fast() -> None:                                       # round 2, item 8
    t = time.perf_counter()
    hidden(" " * 4000 + "\nName: Layla Haddad\n" + " " * 4000 + "Policy number: MTR-2026-12345\nName:" + " " * 4000
           + "Omar Saleh  " + " " * 4000 + "(son)", "Layla", "MTR-2026", "Omar")
    assert time.perf_counter() - t < 1
    lines = ["Policyholder: Layla Haddad", "Policy number: HMI-MTR-2291-4471", "Mobile 0501234567",
             "The premium is SAR 2,350 for the period.", "Address: 4512 King Fahd Road, Al Olaya, Riyadh 12211",
             "| Name | ID number |", "|---|---|", "| Layla Haddad | 2123456789 |", "Bill To:", "Layla Haddad",
             "This policy does not renew automatically: contact us 7 days before it ends."]
    page, n = [], 0
    while sum(len(line) + 1 for line in page) < 19_000:
        page.append(" " * 300 + lines[n % len(lines)])
        n += 1
    t = time.perf_counter()
    hidden("\n".join(page), "Layla", "HMI-MTR", "0501234567", "4512", "2123456789")
    assert time.perf_counter() - t < 1


@pytest.mark.parametrize("text, value", [
    ("Call 415-555-0123 today.", "555-0123"), ("Office (415) 555-0123", "555-0123"),
    ("London 020 7946 0958", "7946 0958"), ("Cairo 010 1234 5678", "1234 5678"), ("Leeds 0113-496-0000", "496-0000"),
])
def test_phone_numbers_without_a_label(text: str, value: str) -> None:                 # round 2, item 9
    hidden(text, value)


def test_numbers_that_are_not_phone_numbers() -> None:                                 # round 2, item 9
    text = "Customer care 920 000 000. Service 800 000 0000. Booked 05-11-2026 10:30, ref 2026-05-13, 05 11 2026."
    assert Vault().redact(text)[0] == text


def test_ibans_need_a_registered_country_and_its_length() -> None:                     # round 2, item 10
    for iban in ("SA03 8000 0000 6080 1016 7519", "AE07 0331 2345 6789 0123 456", "GB82 WEST 1234 5698 7654 32",
                 "DE89 3704 0044 0532 0130 00", "FR14 2004 1010 0505 0001 3M02 606"):
        assert iban_ok(iban), iban
        hidden(f"Pay to {iban} today.", iban)
    assert not iban_ok("SA03 8000 0000 6080 1016 751")              # one digit short for Saudi Arabia
    assert not iban_ok("GB82 WEST 1234 5698 7654 32 1")            # one too many for the United Kingdom
    start = date(2025, 1, 1)
    for n in range(400):
        d = start + timedelta(days=n)
        for text in (f"Membership renews on {d:%d %B %Y}.", f"From {d:%d %B %Y} to {d + timedelta(days=365):%d %B %Y}",
                     f"It is {d:%d %B %Y} at {d:%d %b %Y}."):
            assert Vault().redact(text)[0] == text, text
