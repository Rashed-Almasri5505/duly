from __future__ import annotations

import pytest

from duly.privacy import PrivacyError, Vault, iban_ok, luhn_ok
from duly.samples import load_samples
from tests.conftest import TODAY

PERSONAL = ["Sami Nasser", "1098765432", "+966 50 555 0134", "0505550134", "sami.nasser@example.com",
            "SA03 8000 0000 6080 1016 7519", "Building 4127, Al Yasmin District, Riyadh 13325", "4821 RJA",
            "JTNB11HK0N3045217", "14/03/1994", "HMI-MTR-2291-4471", "PF-88213", "7XK2Q93", "INV-2025-118204",
            "10-2025-0045821", "memberships@pulsefitness.example"]
KEPT = ["Harbor Mutual Insurance", "PULSE FITNESS CLUB", "Al Noor Real Estate Co.", "Northwind Electronics",
        "Dell XPS 13 9350", "SAR 3,600", "10,500", "29 November 2026", "Najm", "920 000 000"]


@pytest.fixture
def redacted() -> tuple[Vault, str]:
    vault = Vault()
    vault.add_known_person("Sami Nasser")
    texts = [vault.redact(s.text, s.title)[0] for s in load_samples(TODAY)]
    return vault, "\n".join(texts)


def test_every_personal_detail_in_the_samples_is_hidden(redacted: tuple[Vault, str]) -> None:
    vault, text = redacted
    for value in PERSONAL:
        assert value not in text, value
    assert "Sami" not in text and "Nasser" not in text      # first and last names on their own too
    assert vault.leaks(text) == []


def test_companies_dates_and_amounts_are_kept(redacted: tuple[Vault, str]) -> None:
    _, text = redacted
    for value in KEPT:
        assert value in text, value


def test_same_number_in_two_formats_gets_one_placeholder() -> None:
    vault = Vault()
    a, _ = vault.redact("Mobile: +966 50 555 0134")
    b, _ = vault.redact("Call 0505550134 today")
    assert a.split(": ")[1] == "[PHONE_1]" and "[PHONE_1]" in b


def test_restore_puts_the_values_back() -> None:
    vault = Vault()
    safe, _ = vault.redact("Policy number: HMI-1234-5678\nEmail: a.person@example.org")
    assert "HMI-1234-5678" not in safe
    assert vault.restore(safe) == "Policy number: HMI-1234-5678\nEmail: a.person@example.org"
    assert vault.restore("ref [ACCOUNT 1]") == "ref HMI-1234-5678"     # tolerant of a model's spacing


def test_leak_check_blocks_a_real_value_and_passes_placeholders() -> None:
    vault = Vault()
    vault.redact("IBAN: SA03 8000 0000 6080 1016 7519")
    with pytest.raises(PrivacyError):
        vault.check_outbound({"messages": ["pay to SA0380000000608010167519"]}, "test")
    vault.check_outbound({"messages": ["pay to [IBAN_1]"]}, "test")


def test_checksums_decide_ibans_and_cards() -> None:
    assert iban_ok("SA03 8000 0000 6080 1016 7519")
    assert not iban_ok("SA04 8000 0000 6080 1016 7519")
    assert luhn_ok("4111 1111 1111 1111")
    assert not luhn_ok("4111 1111 1111 1112")
    vault = Vault()
    safe, _ = vault.redact("card 4111 1111 1111 1111 and order 4111 1111 1111 1112")
    assert "[CARD_1]" in safe and "4111 1111 1111 1112" in safe


def test_greetings_to_teams_are_not_people() -> None:
    vault = Vault()
    safe, used = vault.redact("Dear Dell Support,\nDear Claims Team,\nDear Layla,")
    assert "Dell Support" in safe and "Claims Team" in safe
    assert [e.value for e in used] == ["Layla"]


def test_masking_model_text_adds_nothing_new() -> None:
    vault = Vault()
    vault.add_known_person("Sami Nasser")
    out = vault.mask_known("Dear Dell Support, this is Sami. Call +1 800 624 9896.")
    assert out == "Dear Dell Support, this is [PERSON_1]. Call +1 800 624 9896."
    assert len(vault.entries) == 1


def test_placeholders_are_never_redacted_again() -> None:
    vault = Vault()
    safe, _ = vault.redact("Name: [PERSON_1]\nPolicy number: [ACCOUNT_2]")
    assert safe == "Name: [PERSON_1]\nPolicy number: [ACCOUNT_2]" and not vault.entries
