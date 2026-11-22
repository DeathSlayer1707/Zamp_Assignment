"""Orchestrates one invoice run. Every stage writes events to the DB, which is
what the live run view streams and what the audit trail is built from."""
import hashlib
import json
import time
import traceback
from datetime import timedelta

from . import checks, db, reader
from .config import POLICY, STAGE_DELAY_MS
from .extract import extract
from .normalise import norm_invoice_number, to_date

STAGES = [
    {"key": "intake", "name": "Receive document", "what": "Fingerprint the file so re-sends can be caught."},
    {"key": "read", "name": "Read document", "what": "Use the PDF text layer; fall back to OCR for scans."},
    {"key": "extract", "name": "Extract fields", "what": "Pull vendor, numbers, dates, lines, tax, bank details."},
    {"key": "validate", "name": "Validate invoice", "what": "Mandatory fields, arithmetic, dates, GSTIN checksum."},
    {"key": "vendor", "name": "Verify vendor", "what": "Vendor master match, status, remit-to bank details."},
    {"key": "po", "name": "Match PO and receipt", "what": "Find or infer the PO; check price, received qty, PO balance."},
    {"key": "tax", "name": "Check GST", "what": "CGST+SGST vs IGST by place of supply; rate and amounts."},
    {"key": "duplicates", "name": "Check duplicates", "what": "Same file, same number (normalised), or same amount nearby."},
    {"key": "decide", "name": "Decide and route", "what": "Apply policy, route exceptions, draft messages."},
]
SEV_ORDER = {"pass": 0, "info": 1, "warn": 2, "fail": 3, "block": 4}

QUEUE_PRIORITY = [
    ({"BANK_CHANGED"}, "Treasury (payment security)"),
    ({"VENDOR_GSTIN_MISMATCH", "VENDOR_GSTIN_INVALID", "TAX_TYPE_WRONG", "TAX_RATE", "TAX_AMOUNT", "TAX_MISSING"}, "Tax and compliance"),
    ({"QTY_OVERBILLED", "QTY_NOT_RECEIVED", "PRICE_VARIANCE", "PO_VALUE_EXCEEDED", "LINE_NOT_ON_PO",
      "AMOUNT_VARIANCE", "PO_NOT_FOUND", "PO_VENDOR_MISMATCH", "PO_CLOSED", "PO_UNMATCHED"}, "Procurement"),
    ({"ABOVE_AUTO_LIMIT"}, "Finance controller"),
]


def worst(findings):
    return max((f["severity"] for f in findings), key=lambda s: SEV_ORDER[s], default="pass")


def _pause():
    if STAGE_DELAY_MS:
        time.sleep(STAGE_DELAY_MS / 1000)


def _money(x):
    return checks.money(x)


def decide(ctx, all_findings):
    blocks = [f for f in all_findings if f["severity"] == "block"]
    fails = [f for f in all_findings if f["severity"] == "fail"]
    inv, v, po = ctx["inv"], ctx.get("vendor"), ctx.get("po")
    vname = v["name"] if v else (inv.get("vendor_name") or "Unknown vendor")
    num = inv.get("invoice_number") or "(no number)"
    if blocks:
        codes_b = {f["code"] for f in blocks}
        decision = "rejected"
        queue = ("Procurement (vendor blocked)" if "VENDOR_BLOCKED" in codes_b else
                 "Returned to sender" if codes_b & {"NOT_AN_INVOICE", "UNREADABLE"} else "Returned to vendor")
        summary = f"Rejected. {'; '.join(f['title'] for f in blocks)}."
    elif fails:
        decision = "needs_review"
        codes = {f["code"] for f in fails}
        queue = next((q for c, q in QUEUE_PRIORITY if codes & c), "AP review")
        summary = (f"Held for {queue}. {len(fails)} issue{'s' if len(fails) > 1 else ''} to resolve: "
                   + "; ".join(f["title"] for f in fails) + ".")
    else:
        decision, queue = "approved", "Payment run"
        due = inv.get("due_date")
        if not due and inv.get("invoice_date") and v:
            due = (to_date(inv["invoice_date"]) + timedelta(days=v["payment_terms_days"])).isoformat()
        ctx["pay_by"] = due
        ctx["pay_by_h"] = to_date(due).strftime("%d %b %Y") if due else "the due date"
        summary = (f"Approved for payment. {vname} invoice {num} for {_money(inv.get('total'))} matches "
                   f"{po['po_number']} on price and received quantity. Pay by {ctx['pay_by_h']}.")
    return decision, queue, summary


def communications(ctx, decision, findings):
    inv, v, po = ctx["inv"], ctx.get("vendor"), ctx.get("po")
    codes = {f["code"]: f for f in findings}
    num = inv.get("invoice_number") or "your recent invoice"
    vname = v["name"] if v else inv.get("vendor_name") or "Vendor"
    to_vendor = v["contact_email"] if v else "(vendor email on submission)"
    msgs = []
    sign = "\n\nRegards,\nAccounts Payable\nNorthwind Retail Pvt Ltd"

    if "MISSING_INVOICE_NUMBER" in codes or "MISSING_INVOICE_DATE" in codes:
        missing = [n for c, n in (("MISSING_INVOICE_NUMBER", "invoice number"), ("MISSING_INVOICE_DATE", "invoice date")) if c in codes]
        msgs.append({"audience": "Vendor", "to": to_vendor, "subject": f"Action needed: please re-issue your invoice for {inv.get('po_number') or 'our order'}",
                     "body": f"Hello {vname} team,\n\nWe received your invoice for {_money(inv.get('total'))}"
                             f"{' against ' + inv['po_number'] if inv.get('po_number') else ''}, but it is missing the {' and '.join(missing)}. "
                             "Under GST rules we can only book a tax invoice that carries a unique serial number and date of issue.\n\n"
                             "Please send a corrected invoice to this address. Nothing else needs to change; we will process it as soon as it arrives." + sign})
    if "NOT_AN_INVOICE" in codes or "UNREADABLE" in codes:
        msgs.append({"audience": "Sender", "to": "(sender of the email)", "subject": "We couldn't find an invoice in your attachment",
                     "body": "Hello,\n\nThe document you sent to our invoices mailbox doesn't appear to be a readable invoice"
                             + (" (it looks like a different kind of document)" if "NOT_AN_INVOICE" in codes else " (the scan is too unclear to read)")
                             + ". Please reply with the invoice as a PDF showing the invoice number, date, GSTIN, line items and total." + sign})
    if "DUP_INVOICE" in codes:
        f = codes["DUP_INVOICE"]["evidence"]
        msgs.append({"audience": "Vendor", "to": to_vendor, "subject": f"Invoice {num} - already processed",
                     "body": f"Hello {vname} team,\n\nThe invoice you sent ({num}) matches invoice {f.get('ledger_invoice')} for "
                             f"{_money(f.get('ledger_total'))}, which is already in our books. We have not booked it again.\n\n"
                             "If this is a different invoice, please reply with the distinct invoice number and supporting delivery details." + sign})
    if "VENDOR_UNKNOWN" in codes:
        msgs.append({"audience": "Vendor", "to": "(sender of the email)", "subject": "Vendor registration needed before we can pay",
                     "body": f"Hello,\n\nWe received an invoice from {inv.get('vendor_name')}, but we don't have you registered as a supplier. "
                             "Please complete vendor onboarding (company, GST and bank details) and re-send the invoice afterwards." + sign})
    if "VENDOR_BLOCKED" in codes:
        msgs.append({"audience": "Internal - Procurement", "to": po["buyer_contact"] if po else "Procurement",
                     "subject": f"Invoice received from blocked vendor {vname}",
                     "body": f"An invoice ({num}, {_money(inv.get('total'))}) arrived from {vname}, which is blocked in the vendor master: "
                             f"{v['status_reason'] if v else ''}.\n\nIt has not been booked. Please confirm whether the vendor should be "
                             "re-verified, or let the vendor know we cannot pay until their status is resolved."})
    if "BANK_CHANGED" in codes:
        e = codes["BANK_CHANGED"]["evidence"]
        msgs.append({"audience": "Internal - Treasury", "to": "treasury@northwind.example",
                     "subject": f"HOLD payment: bank change on {vname} invoice {num}",
                     "body": f"Invoice {num} ({_money(inv.get('total'))}) asks us to pay a new account:\n"
                             f"  New:     {e.get('invoice_bank')} a/c {e.get('invoice_account')} IFSC {e.get('invoice_ifsc')}\n"
                             f"  On file: {e.get('master_bank')} a/c {e.get('master_account')} IFSC {e.get('master_ifsc')}\n"
                             f"Vendor note: \"{e.get('vendor_note') or '-'}\"\n\n"
                             f"Before any change: call {vname} on the number already on file ({v['contact_phone']}), not a number from this "
                             "invoice or its email. Ask for a cancelled cheque or bank letter on letterhead, and get a second approver "
                             "to update the vendor master. Until then, pay only to the account on file, or hold."})
    over = [f for c, f in codes.items() if c in ("QTY_OVERBILLED", "PO_VALUE_EXCEEDED", "QTY_NOT_RECEIVED")]
    if over and po:
        msgs.append({"audience": "Internal - PO owner", "to": po["buyer_contact"],
                     "subject": f"{po['po_number']}: invoice {num} exceeds what was ordered/received",
                     "body": f"{vname} has invoiced {num} against {po['po_number']} ({po['description']}).\n\n"
                             + "\n".join(f"- {f['title']}. {f['detail']}" for f in over) +
                             "\n\nCan you confirm whether extra units were delivered (and raise a PO amendment and goods receipt), "
                             "or whether we should ask the vendor for a revised invoice?"})
    if "PRICE_VARIANCE" in codes and po:
        pv = [f for f in findings if f["code"] == "PRICE_VARIANCE"]
        msgs.append({"audience": "Vendor", "to": to_vendor, "subject": f"Invoice {num}: price differs from {po['po_number']}",
                     "body": f"Hello {vname} team,\n\nInvoice {num} bills some items above the agreed PO price:\n"
                             + "\n".join(f"- {f['title']}. {f['detail']}" for f in pv) +
                             f"\n\nPlease send a revised invoice at the PO price, or a credit note for the difference. "
                             "If a price revision was agreed, please share the approval so our buyer can amend the PO." + sign})
    if "PO_INFERRED" in codes and decision != "rejected":
        msgs.append({"audience": "Vendor", "to": to_vendor, "subject": "Please quote our PO number on future invoices",
                     "body": f"Hello {vname} team,\n\nYour invoice {num} didn't carry a purchase order number. We've matched it to "
                             f"{po['po_number'] if po else 'an open PO'} for now. Quoting the PO number on every invoice helps us pay you faster." + sign})
    return msgs


def next_actions(decision, queue, findings, ctx):
    if decision == "approved":
        return [f"Post to ledger against {ctx['po']['po_number']}", f"Schedule payment by {ctx.get('pay_by_h')}",
                "Archive document with this decision record"]
    if decision == "rejected":
        return ["Do not book or pay", "Send the drafted message", "Close the run"]
    acts = [f"Assigned to {queue}"]
    for f in findings:
        if f["severity"] == "fail":
            acts.append({"BANK_CHANGED": "Verify bank change by call-back to number on file; pay only to account on file",
                         "PO_INFERRED": "Confirm the suggested PO, then approve",
                         "QTY_OVERBILLED": "Ask PO owner to confirm extra delivery or request a revised invoice",
                         "QTY_NOT_RECEIVED": "Wait for the goods receipt, or ask the store to record it",
                         "PRICE_VARIANCE": "Get vendor credit note or buyer's PO amendment",
                         "PO_VALUE_EXCEEDED": "PO amendment needed before payment",
                         "LOW_CONFIDENCE": "Verify extracted values against the document",
                         "EXTRACTOR_DISAGREE": "Check the disputed field on the document"}.get(f["code"], f"Resolve: {f['title']}"))
    return list(dict.fromkeys(acts))


def run(run_id: int, path: str, filename: str):
    t0 = time.time()
    ctx = {"run_id": run_id}
    stage_results = {}

    def stage(key, fn):
        meta = next(s for s in STAGES if s["key"] == key)
        db.add_event(run_id, key, "start", "running", meta["name"])
        t = time.time()
        findings, data = fn()
        _pause()
        status = worst(findings) if findings else "pass"
        stage_results[key] = {"status": status, "findings": findings, "data": data,
                              "ms": int((time.time() - t) * 1000)}
        db.add_event(run_id, key, "end", status, meta["name"], {"findings": findings, "data": data,
                                                                  "ms": stage_results[key]["ms"]})
        return findings

    try:
        # 1. intake
        def _intake():
            raw = open(path, "rb").read()
            ctx["file_hash"] = hashlib.sha256(raw).hexdigest()
            db.update_run(run_id, file_hash=ctx["file_hash"])
            return [checks.F("RECEIVED", "pass", f"Received {filename}", f"{len(raw) / 1024:.0f} KB, SHA-256 {ctx['file_hash'][:12]}...")], \
                   {"bytes": len(raw), "sha256": ctx["file_hash"]}
        stage("intake", _intake)

        # 2. read
        def _read():
            doc = reader.read(path)
            ctx["doc"] = doc
            if doc["chars"] < 40:
                return [checks.F("UNREADABLE", "block", "Document has no readable text even after OCR",
                                 "Ask the vendor for a clearer copy.")], {k: doc[k] for k in ("pages", "method", "chars")}
            f = [checks.F("TEXT", "pass" if doc["method"] == "text_layer" else "info",
                          "Read from PDF text layer" if doc["method"] == "text_layer" else
                          f"Scanned image - read with OCR ({doc['ocr_confidence']:.0%} OCR confidence)",
                          f"{doc['pages']} page(s), {doc['chars']} characters.")]
            if doc["method"] == "ocr" and doc["ocr_confidence"] < 0.75:
                f.append(checks.F("OCR_POOR", "fail", "Poor scan quality", "Values may be misread."))
            return f, {"pages": doc["pages"], "method": doc["method"], "chars": doc["chars"],
                       "ocr_confidence": doc["ocr_confidence"], "text": doc["text"][:6000]}
        if any(f["severity"] == "block" for f in stage("read", _read)):
            raise _ShortCircuit()

        # 3. extract
        def _extract():
            ex = extract(ctx["doc"])
            ctx["extraction"] = ex
            inv = ex["invoice"]
            ctx["inv"] = inv
            crit = ["vendor_name", "invoice_number", "invoice_date", "total", "line_items"]
            cs = [inv["_confidence"].get(k) for k in crit if inv["_confidence"].get(k)]
            ctx["confidence"] = round(sum(cs) / len(cs), 3) if cs else 0.0
            found = [k for k in ("vendor_name", "invoice_number", "invoice_date", "po_number", "subtotal", "total") if inv.get(k) is not None]
            f = [checks.F("EXTRACTED", "pass", f"Extracted {len(found)}/6 header fields and {len(inv['line_items'])} line item(s)",
                          f"Method: {ex['method']}. Confidence {ctx['confidence']:.0%}.")]
            if ex.get("llm_error"):
                f.append(checks.F("LLM_FALLBACK", "warn", "LLM unavailable - used deterministic parser", ex["llm_error"]))
            # Is this an invoice at all? (wrong attachment, statement, PO copy, marketing PDF...)
            import re as _re
            signals = {"invoice wording": bool(_re.search(r"(?i)\b(tax\s+invoice|invoice|bill)\b", ctx["doc"]["text"])),
                       "total": inv.get("total") is not None, "line items": bool(inv["line_items"]),
                       "GSTIN": bool(inv.get("vendor_gstin")), "invoice number": bool(inv.get("invoice_number"))}
            if sum(signals.values()) < 3:
                f.append(checks.F("NOT_AN_INVOICE", "block", "This document doesn't look like an invoice",
                                  "Found: " + (", ".join(k for k, v in signals.items() if v) or "none of the usual invoice markers")
                                  + ". Missing: " + ", ".join(k for k, v in signals.items() if not v) + ".", signals=signals))
                ctx["not_invoice"] = True
            missing = [k for k in ("invoice_number", "invoice_date", "po_number", "total") if inv.get(k) is None]
            if missing:
                f.append(checks.F("FIELDS_MISSING", "info", "Not found on document: " + ", ".join(m.replace("_", " ") for m in missing)))
            return f, {"invoice": {k: v for k, v in inv.items()}, "method": ex["method"], "confidence": ctx["confidence"]}
        stage("extract", _extract)
        if ctx.get("not_invoice"):
            db.update_run(run_id, extraction_method=ctx["extraction"]["method"], confidence=ctx["confidence"])
            raise _ShortCircuit()
        db.update_run(run_id, vendor_name=ctx["inv"].get("vendor_name"), invoice_number=ctx["inv"].get("invoice_number"),
                      invoice_number_norm=norm_invoice_number(ctx["inv"].get("invoice_number")),
                      invoice_date=ctx["inv"].get("invoice_date"), total=ctx["inv"].get("total"),
                      currency=ctx["inv"].get("currency"), extraction_method=ctx["extraction"]["method"],
                      confidence=ctx["confidence"])

        stage("validate", lambda: (checks.validate_invoice(ctx), {}))
        stage("vendor", lambda: (checks.verify_vendor(ctx), {"vendor": ctx.get("vendor")}))
        if ctx.get("vendor"):
            db.update_run(run_id, vendor_id=ctx["vendor"]["vendor_id"], vendor_name=ctx["vendor"]["name"])

        def _po():
            f = checks.match_po(ctx)
            po = ctx.get("po")
            return f, {"po": ({k: po[k] for k in ("po_number", "description", "po_date", "total", "billed_value",
                                                 "remaining_value", "buyer_contact")} if po else None),
                       "inferred": ctx.get("po_inferred", False), "lines": ctx.get("line_results", [])}
        stage("po", _po)
        if ctx.get("po"):
            db.update_run(run_id, po_number=ctx["po"]["po_number"])
        stage("tax", lambda: (checks.check_tax(ctx), {}))
        stage("duplicates", lambda: (checks.check_duplicates(ctx), {}))

    except _ShortCircuit:
        ctx.setdefault("inv", {})
    except Exception as e:
        db.add_event(run_id, "error", "error", "block", "Process error", {"error": str(e), "trace": traceback.format_exc()[-2000:]})
        db.update_run(run_id, status="error", summary=f"Process error: {e}", finished_at=db.now(),
                      duration_ms=int((time.time() - t0) * 1000), decision="error")
        return

    # 9. decide
    def _decide():
        lim = checks.approval_limit(ctx) if ctx.get("inv") else []
        all_f = [f for r in stage_results.values() for f in r["findings"]] + lim
        decision, queue, summary = decide(ctx, all_f)
        comms = communications(ctx, decision, all_f)
        acts = next_actions(decision, queue, all_f, ctx)
        ctx.update(decision=decision, queue=queue, summary=summary, comms=comms, actions=acts)
        exc = [f for f in all_f if f["severity"] in ("fail", "block")]
        lim_out = lim + [checks.F("DECISION", {"approved": "pass", "needs_review": "fail", "rejected": "block"}[decision],
                                  {"approved": "Approved for payment", "needs_review": f"Needs review - {queue}",
                                   "rejected": "Rejected"}[decision], summary)]
        return lim_out, {"decision": decision, "queue": queue, "summary": summary, "exceptions": exc,
                         "communications": comms, "next_actions": acts, "pay_by": ctx.get("pay_by")}
    stage("decide", _decide)

    if ctx["decision"] == "approved":
        db.post_to_ledger(run_id, ctx["vendor"]["vendor_id"], ctx["inv"], ctx["po"]["po_number"], ctx.get("allocs", []))
        db.add_event(run_id, "decide", "ledger", "pass", f"Posted to ledger against {ctx['po']['po_number']}")

    doc = ctx.get("doc", {})
    result = {
        "stages": stage_results,
        "invoice": ctx.get("inv"),
        "doc": {k: doc.get(k) for k in ("pages", "method", "chars", "ocr_confidence")},
        "po": stage_results.get("po", {}).get("data", {}).get("po"),
        "allocs": ctx.get("allocs", []),
        "decision": ctx["decision"], "queue": ctx["queue"], "summary": ctx["summary"],
        "communications": ctx["comms"], "next_actions": ctx["actions"], "pay_by": ctx.get("pay_by"),
    }
    db.update_run(run_id, status="done", decision=ctx["decision"], queue=ctx["queue"], summary=ctx["summary"],
                  finished_at=db.now(), duration_ms=int((time.time() - t0) * 1000),
                  result_json=json.dumps(result, default=str))


class _ShortCircuit(Exception):
    pass
