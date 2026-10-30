from __future__ import annotations

from datetime import timedelta

from duly.brief import morning_check, urgency_of
from duly.ics import to_ics
from duly.skills import load_skills
from duly.store import Store
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
