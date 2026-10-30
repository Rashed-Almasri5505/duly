---
name: cancel-before-renewal
description: Stop a membership, subscription or contract from renewing automatically - find the last day to give notice, how notice must be given, and draft the cancellation notice.
metadata:
  requires_tools: list_deadlines search_documents save_draft add_reminder
---

# Cancel before it renews

Use this when the user wants to cancel, stop, or not renew a membership, subscription, lease or contract.

1. Call `list_deadlines` with `within_days` 400 and find the item for this contract. Its "act_by" is the last day
   to give notice (the end of the term minus the notice period the document states).
2. Call `search_documents` for "cancel notice" plus the provider's name, to read exactly how notice must be given
   (e-mail, letter, form at the desk) and what happens if notice is late. Quote it.
3. If the last day to act has already passed, say so plainly and explain what the document says happens next.
4. Call `save_draft` with the cancellation notice: addressed to the provider (use the e-mail placeholder from the
   document if there is one), stating the member or contract number placeholder, the request not to renew, and
   asking for written confirmation.
5. Call `add_reminder` two days before the last day to act, titled "Check the cancellation was confirmed".
6. Answer in this order: the last day to act (date as given by the tools), how to send the notice, what it costs
   if missed (renewal amount from the document), then that the draft and reminder are ready.
