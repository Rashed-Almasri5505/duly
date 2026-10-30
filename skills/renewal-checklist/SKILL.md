---
name: renewal-checklist
description: Prepare to renew an insurance policy, lease or licence before it expires - what is due when, what to have ready, what to compare, and a renewal request draft.
metadata:
  requires_tools: list_deadlines web_search save_draft
---

# Renewal checklist

Use this when the user asks how or when to renew something, or when a policy or contract is about to expire.

1. Call `list_deadlines` with `within_days` 400 and find the item. Note both dates: the expiry (due) date and the
   last day to act ("act_by"), which includes any notice period the document asks for.
2. Call `search_documents` for the renewal and claims terms of this document (discounts, conditions, what happens
   if it lapses). Quote what matters.
3. Call `web_search` for the provider's or authority's official renewal procedure, for example
   "<provider> renew policy online" or "renew <document type> <country> official". Give the links. Do not
   recommend a provider or a price; list what the user should compare instead.
4. Call `save_draft` with a checklist: documents and numbers to have ready (as placeholders), questions to ask,
   things to compare, and the dates.
5. Offer `add_reminder` for the last day to act if the user has none.
6. Answer briefly: the two dates, the three most important steps, the links, and that the checklist is saved.
