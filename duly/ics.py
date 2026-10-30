"""Calendar export: one all-day event on each last day to act, with an alarm some days before.

The file is built on this computer with the real values restored, for the user's own calendar.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from duly.privacy import Vault
from duly.store import KIND_LABELS, Obligation


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Lines longer than 75 octets are folded, as the iCalendar format requires."""
    out, raw = [], line.encode("utf-8")
    while len(raw) > 75:
        cut = 75
        while (raw[cut] & 0xC0) == 0x80:     # do not split a UTF-8 character
            cut -= 1
        out.append(raw[:cut].decode("utf-8"))
        raw = b" " + raw[cut:]
    out.append(raw.decode("utf-8"))
    return "\r\n".join(out)


def to_ics(items: list[Obligation], vault: Vault, lead_days: int, today: date | None = None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Duly//Paperwork deadlines//EN", "CALSCALE:GREGORIAN"]
    for o in items:
        if o.status == "done" or (today and o.due_date < today):
            continue
        day = o.act_by
        title = vault.restore(f"{KIND_LABELS.get(o.kind, 'Deadline')}: {o.title}")
        desc = vault.restore("\n".join(x for x in [o.action, f"Due {o.due_date.isoformat()}",
                                                   f"Document: \"{o.date_quote}\"" if o.date_quote else "",
                                                   f"Rule: \"{o.rule_quote}\"" if o.rule_quote else ""] if x))
        lines += ["BEGIN:VEVENT", f"UID:{o.id}@duly", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
                  f"DTEND;VALUE=DATE:{date.fromordinal(day.toordinal() + 1).strftime('%Y%m%d')}",
                  f"SUMMARY:{_escape(title)}", f"DESCRIPTION:{_escape(desc)}",
                  "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_escape(title)}",
                  f"TRIGGER:-P{max(0, lead_days)}D", "END:VALARM", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
