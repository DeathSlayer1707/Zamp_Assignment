# Design notes: Invoice desk (PS-1)

## 1. Who this is for

The user is an AP clerk at a mid-size Indian company (Northwind Retail, Bengaluru) who opens a few hundred vendor PDFs a month. The buyer is their finance controller, who cares about three things:

1. Don't pay what shouldn't be paid: duplicates, over-billing, fraud.
2. Don't make people re-check what's obviously fine.
3. Be able to explain any payment later, to an auditor.

So the job isn't really "extract fields from a PDF". It's **decide, explain, and route**, with extraction as one step along the way.

## 2. Assumptions

| Assumption | Why |
|---|---|
| Indian GST context (CGST+SGST for intra-state, IGST for inter-state, GSTIN on invoices) | Makes the tax check real, not cosmetic. Zamp works with Indian finance teams. |
| The PO book has line items **and** goods-receipt quantities | Real AP matching is 3-way (invoice, PO, receipt). Without the receipt you can't catch "billed for goods not delivered". |
| An invoice history / ledger exists | Needed for duplicate checks and for split-PO consumption. |
| Tolerance = lesser of 1% or ₹500, per-line price tolerance 1% | A common AP policy shape. Lives in `config.py`, so it's a config change, not a code change. |
| Auto-approval cap of ₹5,00,000 | Approval matrices exist everywhere. Big invoices get a human even when they're clean. |
| Missing invoice number or date = **reject**, not review | Under GST, a tax invoice without these isn't valid (no input tax credit). A human can't fix it; only the vendor can. |
| PO inferred = **review**, not auto-approve | Configurable. The default is cautious because a wrong PO match quietly corrupts PO balances. |
| Messages are drafted, never sent | Human in the loop for anything that leaves the building. |

## 3. Process map

| # | Stage | What it does | Can stop the invoice? |
|---|---|---|---|
| 1 | Receive | SHA-256 fingerprint of the file | - |
| 2 | Read | PDF text layer; if under 80 characters, render at 300 dpi, auto-rotate, OCR with tesseract | Unreadable → reject |
| 3 | Extract | Rules parser always runs. LLM (if a key is set) runs too, and the two are reconciled on the key fields. Also checks whether the document is an invoice at all. | Not an invoice → reject |
| 4 | Validate | Mandatory fields, qty × rate = amount, lines = subtotal, subtotal + tax = total, date sanity, bill-to GSTIN, vendor GSTIN checksum, extraction confidence | Missing number/date → reject; arithmetic → review |
| 5 | Vendor | GSTIN match (fuzzy name as fallback), status, GSTIN consistency, **remit-to bank vs master** | Unknown/blocked → reject; bank change → review (Treasury) |
| 6 | PO + receipt | Explicit PO, or infer from the vendor's open POs (60% description similarity, 40% value fit, needs a clear winner). Per line: price variance, qty vs (received − already billed). Header tolerance. Cumulative PO consumption. | Review (Procurement) |
| 7 | GST | Tax type by place of supply, rate vs PO, amounts | Review (Tax) |
| 8 | Duplicates | Exact file hash; normalised invoice number (`SL/7781` = `SL-7781` = `sl 07781`) against the ledger and the open review queue; same vendor + same amount within 15 days | Exact/normalised → reject; near → review |
| 9 | Decide | Any `block` → Rejected; any `fail` → Needs review, routed by priority (Treasury > Tax > Procurement > Controller > AP); else Approved and posted to the ledger | - |

All checks run even after one fails, so the reviewer sees every problem at once, not one per round trip. The only short-circuits are "unreadable" and "not an invoice", where nothing downstream means anything.

## 4. Key design decisions

**LLM reads, rules decide.** An LLM is great at reading 50 different invoice layouts and bad at being consistently right about whether ₹3,200 is inside a tolerance. So it does extraction only, under a strict JSON schema. Every decision is deterministic code with a reason code. Same invoice, same answer, every time, and an auditor can trace every rupee.

**Two extractors, reconciled.** The rules parser runs on every invoice. When an LLM is configured, the two are compared on invoice number, date, PO, GSTIN, subtotal and total. A disagreement becomes a `fail`, which goes to a human. This is a cheap guard against the worst LLM failure in finance: a confidently wrong number. It also means the system keeps working, and says so, if the LLM is down.

**Arithmetic as an extraction check.** qty × rate = amount, Σ lines = subtotal, subtotal + tax = total. These catch vendor mistakes *and* misreads (OCR turning 8 into 3) for free.

**3-way match with memory.** The ledger stores billed quantity per PO line. That's what makes split invoices work: invoice 2 of a PO is fine, invoice 3 that takes 30 ordered units to 34 is caught, even though each invoice looks fine on its own.

**Route to the person who can fix it.** "Needs review" with no owner just becomes a pile. Bank changes go to Treasury, price/quantity issues to Procurement (with a message to the PO owner), tax problems to Tax.

**Bank change is review, not reject.** Vendors do legitimately change banks. The right control is to hold, verify by calling the number *already on file* (never one from the invoice), and get a second approver for the master change. That's what the drafted Treasury note says.

## 5. Edge cases (and why these four)

| Edge case | What it tests | Outcome |
|---|---|---|
| **Scanned invoice with no PO number** (Greenleaf) | Two uncertainty sources at once: OCR and an inferred match. Shows the system *ranking candidate POs* and explaining the pick, then deliberately not auto-approving. | Needs review, PO-4504 suggested (score 100% vs 0% for the other open PO) |
| **Split PO / over-billing** (Bluestar invoice 2, then 3) | Statefulness. The same PO is approved once and then caught when cumulative billing passes the PO and the goods receipt. Run in order in the demo. | Invoice 2 approved; invoice 3 held for Procurement (34 of 30 units) |
| **Disguised duplicate** (Swift SL/7781) | A naive check (same file or same string) misses it: new layout, different hash, `SL/7781` vs `SL-7781`. Caught by normalising the number. | Rejected, with a vendor note |
| **Bank details changed** (Swift SL-8042) | The most expensive AP failure, business email compromise. Everything else is perfect, which is exactly what makes it dangerous. | Needs review, Treasury, with a call-back protocol |

Extras in the inbox: price creep above tolerance (Procurement), blocked vendor (rejected), missing number and date (rejected, re-issue requested), and re-uploading any processed file (exact duplicate). Uploading a non-invoice PDF gets rejected as "not an invoice".

## 6. Pros and cons

**Pros**
- Actually runs end-to-end on real PDFs, including image-only scans, with no API key required.
- Deterministic, explainable decisions, each with a reason code, evidence, owner and next step. Audit-ready.
- Catches the costly failure modes (duplicates in disguise, over-billing across split invoices, bank-change fraud), not just "fields missing".
- LLM-optional and provider-agnostic. It degrades gracefully and the fallback is visible.
- Policy lives in one config file. Reviewer approvals need a written reason and update PO balances.

**Cons / limitations**
- The rules parser is tuned to label-based layouts. Truly novel layouts need the LLM path, and that path is tested with mocks here, not against a live key.
- Line matching is fuzzy text similarity. "HP 26A toner" vs a PO saying "CF226A cartridge" would miss without item codes or embeddings.
- Single currency (INR), single entity, single-page invoices. Credit notes, advances, TDS and multi-currency aren't handled.
- Reference data is CSV and SQLite. No ERP integration, no concurrency control beyond a lock, and the data resets on free hosting.
- Confidence scores are heuristic (label found + OCR confidence), not calibrated probabilities.
- No email ingestion; upload is manual.

## 7. How I'd make it better

1. **Ingestion**: an IMAP/Gmail watcher or forwarding address, so invoices arrive the way they do today. Split multi-invoice PDFs.
2. **Extraction**: LLM with page images as the primary path, and bounding boxes for each field so the UI can highlight *where* on the PDF a value came from. Per-vendor few-shot memory for repeat layouts.
3. **Matching**: item-code and HSN matching first, embeddings second. Treat PO inference as an assignment problem across all open invoices.
4. **Learning from reviewers**: log every override. If reviewers approve the same exception 20 times (say, a 1.5% freight surcharge from one vendor), suggest a policy change. Don't silently learn it.
5. **Controls**: TDS deduction, MSME 45-day payment rule, GSTR-2B reconciliation (is the vendor's GST actually filed?), vendor bank verification via a penny-drop API.
6. **Ops**: Postgres, a job queue (so 500 invoices on month-end don't block), ERP connectors (Tally/SAP/NetSuite), role-based access, SLA timers on the review queue.
7. **Measure it**: a labelled eval set of 200+ real invoices; track field-level extraction accuracy, false approvals (the metric that matters) and touchless rate over time.

## 8. Demo script (5 min)

Before recording: POs and vendors page, then **Reset demo data**.

1. **0:00 Frame (20s).** "AP clerks match invoices to POs by hand. This takes a PDF and gives a reasoned decision. The LLM reads; rules decide."
2. **0:20 Happy path (60s).** Acme clean invoice. Narrate the steps as they light up. Open Fields (per-field confidence), then PO match (3-way table). "Approved, posted to ledger, pay by 18 Dec."
3. **1:20 Split PO (60s).** Bluestar invoice 2 → approved, "24 of 30 now billed". Then invoice 3 → held. "Each invoice is fine alone; it's the cumulative that's wrong." Show the message drafted to the PO owner.
4. **2:20 Bank change (50s).** Swift SL-8042. "Everything matches, which is why this fraud works." Show the Treasury note and the call-back rule.
5. **3:10 Scanned, no PO (40s).** OCR step, inferred PO with the candidate ranking. "Two uncertainties, so a human confirms." Go to the review queue and approve with a comment.
6. **3:50 Disguised duplicate (25s).** SL/7781 rejected as SL-7781, already paid.
7. **4:15 Dashboard (30s).** Touchless rate, value held, value stopped, top exceptions. End with: "Policy lives in one config file; every decision is in the audit trail."

Have ready for the interview: the "How it decides" page (policy table), `config.py` to show a live tolerance change, and uploading a random non-invoice PDF to show the "not an invoice" gate.
