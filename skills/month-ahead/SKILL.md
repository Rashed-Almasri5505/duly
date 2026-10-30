---
name: month-ahead
description: Give a clear plan of everything due in the coming weeks across all documents - payments, renewals, notice deadlines - in date order, with what to do for each.
metadata:
  requires_tools: list_deadlines
---

# The month ahead

Use this when the user asks what is coming up, what they need to do, or to plan the next weeks.

1. Call `list_deadlines` with `within_days` set to the period the user asked for (default 31).
2. Order the items by their last day to act. For each, give: the date as the tool wrote it, what it is, the amount
   if any, and the one action to take. Mark items whose last day is within 7 days as urgent.
3. Point out clashes (two payments in the same week) and anything that renews automatically, since doing nothing
   means paying again.
4. Do not invent items: if the list is empty, say nothing is due in that period.
5. Offer to draft any notice or to add reminders; do not draft unless the user asks.
