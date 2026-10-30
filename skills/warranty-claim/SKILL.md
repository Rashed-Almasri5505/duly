---
name: warranty-claim
description: Check whether a broken or faulty product is still under warranty, find the manufacturer's official claim procedure, and draft the claim message.
metadata:
  requires_tools: list_deadlines web_search save_draft
---

# Warranty claim

Use this when the user says a product is broken, faulty, or not working, or asks if something is covered.

1. Find the purchase document: call `search_documents` with the product name and the word "warranty".
2. Find the warranty end date: call `list_deadlines` with `within_days` 400 and pick the warranty item for this
   product. If there is none, say the documents do not state an end date.
3. Decide coverage from the document text only:
   - covered if today is on or before the warranty end date and the fault is not excluded;
   - name any exclusion the document lists (accidental damage, liquid, cosmetic) that could apply.
   Quote the document's own words for the end date and the exclusions.
4. Call `web_search` for the manufacturer's official warranty or support page, for example
   "<brand> <product> warranty claim support". Prefer the manufacturer's own site. Give the links.
5. Call `save_draft` with a short claim message to the manufacturer or the store. Include: the product, the
   serial number or service tag placeholder from the document, the purchase date, the invoice number placeholder,
   a plain description of the fault, and what the user asks for (repair or replacement). Leave `to` empty unless
   a document or search result gives the manufacturer's or store's own address; the claim is usually opened
   on the support site found in step 4.
6. Answer in this order: covered or not (with the date and quote), the steps to claim, the links,
   then say the draft is ready to review. Suggest backing up data before handing a device in.
