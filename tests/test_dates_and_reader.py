from __future__ import annotations

import json
from datetime import date, timedelta

from duly.dates import date_in_text, find_dates, slash_convention
from duly.reader import check_item, read_document
from duly.recorded import load_recording, recorded_obligations
from duly.samples import load_samples
from duly.store import Store
from tests.conftest import TODAY, fake_llm, message


def test_dates_are_found_with_the_documents_convention() -> None:
    samples = {s.slug: s for s in load_samples(TODAY)}
    laptop = [d for d, _ in find_dates(samples["01_laptop_receipt"].text)]
    assert laptop == [TODAY - timedelta(days=300), TODAY + timedelta(days=65)]     # 03/01/2027 is 3 January
    gym = [d for d, _ in find_dates(samples["03_gym_membership"].text)]
    assert TODAY + timedelta(days=24) in gym
    lease = [d for d, _ in find_dates(samples["04_apartment_lease"].text)]
    assert TODAY + timedelta(days=12) in lease and TODAY + timedelta(days=195) in lease


def test_month_first_documents_are_recognized() -> None:
    assert slash_convention("Due 12/25/2026, paid 01/02/2026") == "mdy"
    assert slash_convention("Due 25/12/2026") == "dmy"
    assert slash_convention("Due 03/01/2027") == "dmy"           # ambiguous: day first
    assert date_in_text(date(2026, 12, 25), "renews on December 25th, 2026")
    assert not date_in_text(date(2027, 3, 1), "valid until 03/01/2027")


def test_a_notice_period_must_be_written_in_the_document() -> None:
    text = "The term ends on 23 November 2026.\nGive notice at least 14 days before the end of the term."
    candidates = {"D1": (date(2026, 11, 23), "The term ends on 23 November 2026.")}
    ok = check_item({"date_id": "D1", "kind": "renewal", "title": "Ends", "notice_days": 14, "rule_quote": None,
                     "status": "upcoming"}, candidates, text)
    assert ok.verified and ok.act_by == date(2026, 11, 9) and "14 days" in ok.rule_quote
    bad = check_item({"date_id": "D1", "kind": "renewal", "title": "Ends", "notice_days": 30, "rule_quote": None,
                      "status": "upcoming"}, candidates, text)
    assert not bad.verified and "30 days" in bad.check_note
    assert check_item({"date_id": "D9", "title": "x"}, candidates, text) is None      # unknown date ID


def test_reader_sends_only_redacted_text_and_stores_checked_obligations(tmp_path, cfg) -> None:
    store = Store(tmp_path)
    gym = next(s for s in load_samples(TODAY) if s.slug == "03_gym_membership")
    doc = store.add_document(gym.title, gym.text)

    def script(model, messages, kwargs):
        listing = messages[-1]["content"].split("Dates written in the document:\n")[1]
        did = next(line.split(":")[0] for line in listing.splitlines() if (TODAY + timedelta(days=24)).isoformat() in line)
        return message(json.dumps({"obligations": [{
            "date_id": did, "kind": "renewal", "title": "Gym membership renews", "notice_days": 14,
            "rule_quote": "unless the member gives written notice of cancellation at least 14 days before the end of the term.",
            "amount": 3600, "currency": "SAR", "auto_renews": True, "action": "Cancel in writing.", "status": "upcoming"}],
            "counterparty": "Pulse Fitness Club", "summary": "Gym membership agreement."}))

    llm, client = fake_llm(cfg, script)
    read_document(store, llm, doc, TODAY, cfg)
    sent = json.dumps(client.requests[0]["messages"])
    assert "Sami" not in sent and "0505550134" not in sent and "PF-88213" not in sent
    (item,) = store.obligations()
    assert item.verified and item.act_by == TODAY + timedelta(days=10) and item.notice_days == 14
    assert store.outbound()[0].destination.startswith("Nebius Token Factory")


def test_recorded_reading_is_redated_for_any_day(tmp_path) -> None:
    recording = load_recording()
    later = TODAY + timedelta(days=40)
    summary, items = recorded_obligations("03_gym_membership", "doc-x", later, recording)
    (item,) = items
    assert item.due_date == later + timedelta(days=24)
    assert date_in_text(item.due_date, item.date_quote)
