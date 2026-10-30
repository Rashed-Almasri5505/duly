from __future__ import annotations

from datetime import timedelta

from duly.brief import morning_check, urgency_of
from duly.ics import to_ics
from duly.skills import load_skills
from duly.store import Profile, Store
from tests.conftest import TODAY


def test_memory_persists_and_forgets(tmp_path) -> None:
    store = Store(tmp_path)
    note = store.add_note("Prefers reminders two weeks ahead", source="you")
    again = Store(tmp_path)
    assert [n.text for n in again.notes()] == ["Prefers reminders two weeks ahead"]
    assert again.forget_note(note.id) and again.notes() == []


def test_deleting_a_document_deletes_its_dates_and_wipe_deletes_everything(sample_store) -> None:
    gym = next(d for d in sample_store.documents() if "Gym" in d.title)
    sample_store.delete_document(gym.id)
    assert all(o.doc_id != gym.id for o in sample_store.obligations())
    sample_store.wipe()
    assert not sample_store.documents() and not sample_store.vault.entries


def test_deleting_a_document_removes_the_personal_details_only_it_used(tmp_path) -> None:
    store = Store(tmp_path)
    store.save_profile(Profile(name="Sami Nasser"))
    lease = store.add_document("Lease", "Tenant: Sami Nasser\nLandlord: Khalid Haddad\nMobile: 0501234567\n"
                                        "Contract number: EJ-55-77821\nIBAN: SA03 8000 0000 6080 1016 7519")
    gym = store.add_document("Gym", "Member name: Sami Nasser\nMobile: 0501234567")
    store.add_note("Pay the deposit to SA03 8000 0000 6080 1016 7519.", source="you")
    store.redact("My passport number is N12345678.", source="your question")
    gone = sorted([store.vault.lookup("PERSON", "Khalid Haddad"), store.vault.lookup("ACCOUNT", "EJ-55-77821")])
    counters = dict(store.vault.counters)

    assert store.delete_document(lease.id) == gone
    on_disk = (tmp_path / "vault.json").read_text(encoding="utf-8")
    assert "Khalid" not in on_disk and "Haddad" not in on_disk and "EJ-55-77821" not in on_disk
    vault = Store(tmp_path).vault
    assert vault.lookup("PERSON", "Sami Nasser")                     # in the profile
    assert vault.lookup("PHONE", "0501234567")                       # in the gym document
    assert vault.lookup("IBAN", "SA0380000000608010167519")          # in a remembered fact
    assert vault.lookup("NATIONAL_ID", "N12345678")                  # typed in a question, not in the lease
    assert vault.restore(gym.redacted) == "Member name: Sami Nasser\nMobile: 0501234567"
    assert vault.counters == counters and vault.known_people == ["Sami Nasser"]
    car = store.add_document("Car", "Policyholder: Nora Aziz")
    assert car.redacted == f"Policyholder: [PERSON_{counters['PERSON'] + 1}]"   # Khalid's number is not reused


def test_two_processes_on_one_folder_never_give_one_placeholder_to_two_people(tmp_path) -> None:
    app, mcp = Store(tmp_path), Store(tmp_path)          # the app and the MCP server, both open before either writes
    lease = app.add_document("Lease", "Tenant: Layla Haddad\nMobile: 0501234567")
    before_mcp = (tmp_path / "vault.json").read_text(encoding="utf-8")
    note = mcp.add_note("Emergency contact: Omar Saleh\nPassport: N12345678", source="you")
    assert note.text == "Emergency contact: [PERSON_2]\nPassport: [NATIONAL_ID_1]"
    later = Store(tmp_path)
    assert later.vault.restore(lease.redacted) == "Tenant: Layla Haddad\nMobile: 0501234567"
    assert later.vault.restore(note.text) == "Emergency contact: Omar Saleh\nPassport: N12345678"

    # A counter never goes down, even when vault.json was last written by a copy that knew fewer values.
    (tmp_path / "vault.json").write_text(before_mcp, encoding="utf-8")
    gym = later.add_document("Gym", "Member name: Nora Aziz")
    assert gym.redacted == "Member name: [PERSON_3]"                 # [PERSON_2] stays Omar's in the note
    assert "Nora" not in Store(tmp_path).vault.restore(note.text)


def test_the_mcp_server_reads_the_folder_again_for_every_call(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DULY_DATA_DIR", str(tmp_path))
    from duly import mcp_server

    app = Store(tmp_path)
    app.add_note("Layla prefers e-mail.")                 # written by Duly's model: no known value in it yet
    lease = app.add_document("Lease", "Tenant: Layla Haddad")
    found = mcp_server.search_documents("prefers e-mail")     # masked with the vault as it is now
    assert "Layla" not in found and "[PERSON_1] prefers e-mail." in found
    mcp_server.remember("Spouse: Mona Saleh")
    later = Store(tmp_path)
    assert later.vault.restore(lease.redacted) == "Tenant: Layla Haddad"
    assert later.vault.restore(later.notes()[-1].text) == "Spouse: Mona Saleh"


def test_morning_check_works_offline_and_orders_by_urgency(sample_store, cfg) -> None:
    brief = morning_check(sample_store, None, TODAY, cfg)
    assert brief.written_by == "template"
    assert [a.title for a in brief.alerts][0].lower().startswith("gym")
    assert "Mon 9 Nov 2026" in brief.text
    later = morning_check(sample_store, None, TODAY + timedelta(days=5), cfg)
    assert later.alerts[0].urgency == "this week"
    assert urgency_of(TODAY - timedelta(days=1), TODAY, 7) == "overdue"


def test_calendar_file_has_one_event_per_open_date(sample_store) -> None:
    items = sample_store.obligations()
    ics = to_ics(items, sample_store.vault, 7, TODAY)
    assert ics.count("BEGIN:VEVENT") == len(items)
    assert "DTSTART;VALUE=DATE:20261109" in ics and "TRIGGER:-P7D" in ics
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))


def test_skills_declare_tools_that_exist() -> None:
    from duly.tools import tool_schemas
    skills = load_skills()
    names = {t["function"]["name"] for t in tool_schemas(list(skills))}
    assert {"warranty-claim", "cancel-before-renewal", "renewal-checklist", "month-ahead"} <= set(skills)
    for s in skills.values():
        assert s.description and set(s.requires_tools) <= names, s.name
