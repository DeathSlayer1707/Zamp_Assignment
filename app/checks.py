"""Deterministic AP controls. The LLM reads the document; these rules decide.

Every check returns findings: {code, severity, title, detail, evidence}
severity: pass | info | warn | fail | block
  fail  -> invoice needs a human (NEEDS REVIEW)
  block -> invoice cannot be paid as submitted (REJECTED)
"""
from datetime import date, timedelta

from rapidfuzz import fuzz

from . import db, gstin, reference
from .config import BUYER, POLICY
from .normalise import norm_invoice_number, to_date


def F(code, severity, title, detail="", **evidence):
    return {"code": code, "severity": severity, "title": title, "detail": detail, "evidence": evidence}


def money(x):
    """Indian digit grouping, e.g. ₹1,43,842.00"""
    if x is None:
        return "-"
    neg, x = x < 0, abs(round(float(x), 2))
    whole, frac = f"{x:.2f}".split(".")
    if len(whole) > 3:
        head, tail, groups = whole[:-3], whole[-3:], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups + [tail])
    return f"{'-' if neg else ''}₹{whole}.{frac}"


def close(a, b, slack=None):
    slack = POLICY["rounding_slack"] if slack is None else slack
    return a is not None and b is not None and abs(a - b) <= slack


# ------------------------------------------------------------------ stage: validate
def validate_invoice(ctx) -> list[dict]:
    inv, out = ctx["inv"], []
    ext = ctx["extraction"]

    # Mandatory fields. Under GST, a tax invoice without a serial number or date is not valid
    # (no input tax credit), so these block rather than hold.
    if not inv.get("invoice_number"):
        out.append(F("MISSING_INVOICE_NUMBER", "block", "Invoice number missing",
                     "A GST tax invoice must carry a unique serial number. Vendor must re-issue."))
    else:
        out.append(F("INVOICE_NUMBER", "pass", f"Invoice number {inv['invoice_number']}"))
    if not inv.get("invoice_date"):
        out.append(F("MISSING_INVOICE_DATE", "block", "Invoice date missing",
                     "Date of issue is mandatory on a tax invoice and drives payment terms."))
    if not inv.get("line_items"):
        out.append(F("NO_LINE_ITEMS", "fail", "No line items could be read",
                     "Cannot match quantities or prices to the PO."))

    # Total: derive if missing but computable
    if inv.get("total") is None:
        if inv.get("subtotal") is not None and inv.get("tax_total") is not None:
            inv["total"] = round(inv["subtotal"] + inv["tax_total"], 2)
            out.append(F("TOTAL_DERIVED", "warn", "Total not printed - derived from subtotal + tax",
                         f"Derived total {money(inv['total'])}."))
        else:
            out.append(F("MISSING_TOTAL", "fail", "Invoice total could not be determined"))

    # Arithmetic integrity - catches both vendor errors and extraction errors
    bad_lines = [li for li in inv.get("line_items", [])
                 if li.get("qty") is not None and li.get("unit_price") is not None
                 and not close(li["qty"] * li["unit_price"], li.get("amount"))]
    if bad_lines:
        out.append(F("LINE_ARITHMETIC", "fail", f"{len(bad_lines)} line(s) where qty x rate != amount",
                     lines=[li["description"] for li in bad_lines]))
    line_sum = round(sum(li.get("amount") or 0 for li in inv.get("line_items", [])), 2)
    if inv.get("subtotal") is None and inv.get("line_items"):
        inv["subtotal"] = line_sum
        out.append(F("SUBTOTAL_DERIVED", "info", "Subtotal not printed - using sum of lines"))
    elif inv.get("line_items"):
        if close(line_sum, inv["subtotal"]):
            out.append(F("LINES_SUM", "pass", "Line items add up to the subtotal", f"{money(line_sum)}"))
        else:
            out.append(F("LINES_SUM", "fail", "Line items don't add up to the subtotal",
                         f"Lines sum to {money(line_sum)}, invoice says {money(inv['subtotal'])}."))
    if inv.get("subtotal") is not None and inv.get("total") is not None:
        expect = round(inv["subtotal"] + (inv.get("tax_total") or 0), 2)
        if close(expect, inv["total"]):
            out.append(F("TOTAL_SUM", "pass", "Subtotal + tax = total", money(inv["total"])))
        else:
            out.append(F("TOTAL_SUM", "fail", "Subtotal + tax doesn't equal the total",
                         f"Expected {money(expect)}, invoice says {money(inv['total'])}."))

    # Dates
    d = to_date(inv.get("invoice_date"))
    if d:
        today = date.today()
        if d > today:
            out.append(F("FUTURE_DATED", "fail", "Invoice is dated in the future", d.isoformat()))
        elif (today - d).days > POLICY["max_invoice_age_days"]:
            out.append(F("STALE_INVOICE", "fail", f"Invoice is older than {POLICY['max_invoice_age_days']} days"))
        else:
            out.append(F("INVOICE_DATE", "pass", f"Invoice dated {d.strftime('%d %b %Y')}"))

    # Addressed to us?
    if inv.get("buyer_gstin") == BUYER["gstin"]:
        out.append(F("BILL_TO", "pass", "Billed to Northwind's GSTIN", BUYER["gstin"]))
    else:
        out.append(F("BILL_TO", "warn", "Northwind's GSTIN not found on invoice",
                     "Input tax credit may be at risk; confirm the bill-to entity."))

    # Vendor GSTIN well-formed
    ok, why = gstin.validate(inv.get("vendor_gstin"))
    if ok:
        out.append(F("VENDOR_GSTIN_VALID", "pass", "Vendor GSTIN format and checksum valid", inv["vendor_gstin"]))
    else:
        out.append(F("VENDOR_GSTIN_INVALID", "fail", f"Vendor GSTIN {why}", inv.get("vendor_gstin") or ""))

    # Extraction quality
    conf = ctx["confidence"]
    if conf < POLICY["min_extraction_confidence"]:
        out.append(F("LOW_CONFIDENCE", "fail", f"Low extraction confidence ({conf:.0%})",
                     "A human should verify the extracted values against the document."))
    for dsg in ext.get("disagreements", []):
        out.append(F("EXTRACTOR_DISAGREE", "fail", f"Extractors disagree on {dsg['field']}",
                     f"LLM read {dsg['llm']}, rules read {dsg['rules']}.", **dsg))
    return out


# ------------------------------------------------------------------ stage: vendor
def verify_vendor(ctx) -> list[dict]:
    inv, out = ctx["inv"], []
    v, method, score = reference.find_vendor(inv.get("vendor_name"), inv.get("vendor_gstin"))
    if not v or (method == "name" and score < POLICY["vendor_name_match"]):
        out.append(F("VENDOR_UNKNOWN", "block", "Vendor is not in the vendor master",
                     f"'{inv.get('vendor_name')}' could not be matched. Payment requires onboarding first.",
                     best_guess=v["name"] if v else None, score=score))
        return out
    ctx["vendor"] = v
    out.append(F("VENDOR_MATCH", "pass", f"Matched to {v['name']} ({v['vendor_id']})",
                 f"Matched by {'GSTIN' if method == 'gstin' else f'name ({score:.0f}% similar)'}."))
    if v["status"] != "approved":
        out.append(F("VENDOR_BLOCKED", "block", f"Vendor status is '{v['status']}'", v["status_reason"]))
    else:
        out.append(F("VENDOR_ACTIVE", "pass", "Vendor is approved for payment"))
    if inv.get("vendor_gstin") and inv["vendor_gstin"] != v["gstin"]:
        out.append(F("VENDOR_GSTIN_MISMATCH", "fail", "GSTIN on invoice differs from vendor master",
                     f"Invoice {inv['vendor_gstin']} vs master {v['gstin']}."))

    # Remit-to bank details: the single most expensive field to get wrong.
    bank = inv.get("bank") or {}
    acct, ifsc = bank.get("account"), bank.get("ifsc")
    if not acct and not ifsc:
        out.append(F("BANK_NOT_ON_INVOICE", "info", "No bank details on invoice",
                     f"Payment will go to the account on file ({v['bank_name']} ...{v['bank_account'][-4:]})."))
    elif acct == v["bank_account"] and (not ifsc or ifsc == v["ifsc"]):
        out.append(F("BANK_MATCH", "pass", "Remit-to bank details match vendor master",
                     f"{v['bank_name']} ...{v['bank_account'][-4:]}"))
    else:
        change_note = next((n for n in inv.get("notes", []) if "bank" in n.lower() or "account" in n.lower()), None)
        out.append(F("BANK_CHANGED", "fail", "Bank details on invoice differ from vendor master",
                     "Do not pay to the new account. Verify by calling the vendor on the number already on file "
                     f"({v['contact_phone']}), never a number from the invoice or email.",
                     invoice_account=acct, invoice_ifsc=ifsc, invoice_bank=bank.get("bank_name"),
                     master_account=v["bank_account"], master_ifsc=v["ifsc"], master_bank=v["bank_name"],
                     vendor_note=change_note))
    return out


# ------------------------------------------------------------------ stage: tax
def check_tax(ctx) -> list[dict]:
    inv, v, out = ctx["inv"], ctx.get("vendor"), []
    vendor_state = (v["state_code"] if v else (inv.get("vendor_gstin") or "")[:2]) or None
    if not vendor_state:
        return [F("TAX_SKIPPED", "info", "Vendor state unknown - GST type not checked")]
    intra = vendor_state == BUYER["state_code"]
    expected_types = {"CGST", "SGST"} if intra else {"IGST"}
    charged = {t["type"] for t in inv.get("tax_lines", [])}
    place = "same state" if intra else "different states"
    if not charged:
        out.append(F("TAX_MISSING", "fail", "No GST charged",
                     f"Vendor and buyer are in {place}; expected {' + '.join(sorted(expected_types))}."))
        return out
    if charged == expected_types:
        out.append(F("TAX_TYPE", "pass", f"{' + '.join(sorted(charged))} is correct for an {'intra' if intra else 'inter'}-state supply",
                     f"Vendor state {vendor_state}, buyer state {BUYER['state_code']}."))
    else:
        out.append(F("TAX_TYPE_WRONG", "fail", f"Wrong GST type: charged {' + '.join(sorted(charged))}",
                     f"Vendor and buyer are in {place}; expected {' + '.join(sorted(expected_types))}."))
    # rate and amount
    rate_expected = ctx.get("po_tax_rate")
    total_rate = sum(t["rate"] for t in inv["tax_lines"])
    if rate_expected is not None and abs(total_rate - rate_expected) > 0.01:
        out.append(F("TAX_RATE", "fail", f"GST rate {total_rate:g}% differs from PO rate {rate_expected:g}%"))
    elif rate_expected is not None:
        out.append(F("TAX_RATE", "pass", f"GST rate {total_rate:g}% matches the PO"))
    sub = inv.get("subtotal") or 0
    bad = [t for t in inv["tax_lines"] if not close(round(sub * t["rate"] / 100, 2), t["amount"])]
    if bad:
        out.append(F("TAX_AMOUNT", "fail", "GST amount doesn't match rate x taxable value",
                     "; ".join(f"{t['type']} {money(t['amount'])} vs expected {money(sub * t['rate'] / 100)}" for t in bad)))
    else:
        out.append(F("TAX_AMOUNT", "pass", "GST amounts compute correctly", money(inv.get("tax_total"))))
    return out


# ------------------------------------------------------------------ stage: PO match
def _match_lines(inv_lines, po_lines):
    """Greedy best match of invoice lines to PO lines by description similarity."""
    pairs, used = [], set()
    for li in inv_lines:
        best, best_s = None, 0
        for pl in po_lines:
            if pl["line_no"] in used:
                continue
            s = fuzz.token_set_ratio(li["description"].lower(), pl["description"].lower())
            if s > best_s:
                best, best_s = pl, s
        if best and best_s >= POLICY["line_desc_match"]:
            used.add(best["line_no"])
            pairs.append((li, best, best_s))
        else:
            pairs.append((li, None, best_s))
    return pairs


def _score_po(inv, po):
    pairs = _match_lines(inv["line_items"], po["lines"])
    if not pairs:
        return 0.0, pairs
    desc = sum(s for _, pl, s in pairs if pl) / (100 * len(pairs))
    expected = sum(li["qty"] * pl["unit_price"] for li, pl, _ in pairs if pl)
    sub = inv.get("subtotal") or 1
    amount_fit = max(0.0, 1 - abs(expected - sub) / sub) if expected else 0.0
    return round(0.6 * desc + 0.4 * amount_fit, 3), pairs


def match_po(ctx) -> list[dict]:
    inv, v, out = ctx["inv"], ctx.get("vendor"), []
    po_num = inv.get("po_number")
    po = reference.po_with_balances(po_num) if po_num else None
    inferred = False

    if po_num and not po:
        out.append(F("PO_NOT_FOUND", "fail", f"{po_num} doesn't exist in the PO book",
                     "Trying to infer the PO from vendor and line items instead."))
    if po and v and po["vendor_id"] != v["vendor_id"]:
        out.append(F("PO_VENDOR_MISMATCH", "fail", f"{po_num} belongs to a different vendor",
                     f"PO vendor {po['vendor_id']}, invoice vendor {v['vendor_id']}."))
        po = None

    if not po:
        if not v or not inv.get("line_items"):
            out.append(F("PO_UNMATCHED", "fail", "No PO could be matched"))
            return out
        cands = [reference.po_with_balances(p["po_number"]) for p in reference.purchase_orders().values()
                 if p["vendor_id"] == v["vendor_id"] and p["status"] == "open"]
        scored = sorted(((*_score_po(inv, c), c) for c in cands), key=lambda t: -t[0])
        ranking = [{"po": c["po_number"], "score": s, "description": c["description"]} for s, _, c in scored]
        if scored and scored[0][0] >= 0.75 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.15):
            po, inferred = scored[0][2], True
            sev = "warn" if POLICY["auto_approve_inferred_po"] else "fail"
            out.append(F("PO_INFERRED", sev, f"No PO on invoice - inferred {po['po_number']} ({scored[0][0]:.0%} match)",
                         f"Best of {len(scored)} open PO(s) for this vendor by line descriptions and value. "
                         "A reviewer should confirm before posting.", candidates=ranking))
        else:
            out.append(F("PO_UNMATCHED", "fail", "No PO reference and no confident match",
                         f"{len(scored)} open PO(s) for vendor, none clearly fits.", candidates=ranking))
            return out
    else:
        out.append(F("PO_FOUND", "pass", f"{po['po_number']} found: {po['description']}",
                     f"Raised {po['po_date']} by {po['buyer_contact']}, value {money(po['total'])}."))

    ctx["po"] = po
    ctx["po_inferred"] = inferred
    if po["status"] != "open":
        out.append(F("PO_CLOSED", "fail", f"{po['po_number']} is {po['status']}"))
    d = to_date(inv.get("invoice_date"))
    if d and d < to_date(po["po_date"]):
        out.append(F("INVOICE_BEFORE_PO", "warn", "Invoice is dated before the PO",
                     "Possible after-the-fact PO. Check with the buyer."))

    rates = {pl["tax_rate"] for pl in po["lines"]}
    ctx["po_tax_rate"] = rates.pop() if len(rates) == 1 else None

    # 3-way match: invoice line vs PO line (price) vs goods receipt (qty)
    pairs = _match_lines(inv["line_items"], po["lines"])
    allocs, line_results, expected_value = [], [], 0.0
    price_fail = False
    for li, pl, s in pairs:
        r = {"invoice_line": li["description"], "qty": li["qty"], "unit_price": li["unit_price"]}
        if not pl:
            out.append(F("LINE_NOT_ON_PO", "fail", f"'{li['description']}' is not on {po['po_number']}"))
            r["status"] = "not on PO"
            line_results.append(r)
            continue
        r.update(po_line=pl["line_no"], po_description=pl["description"], po_price=pl["unit_price"],
                 ordered=pl["qty_ordered"], received=pl["qty_received"], billed_before=pl["qty_billed"],
                 billable=pl["qty_billable"], similarity=s)
        expected_value += li["qty"] * pl["unit_price"]
        var = (li["unit_price"] - pl["unit_price"]) / pl["unit_price"] * 100
        r["price_var_pct"] = round(var, 2)
        if var > POLICY["unit_price_tolerance_pct"]:
            price_fail = True
            out.append(F("PRICE_VARIANCE", "fail",
                         f"Price above PO on '{pl['description']}': {var:+.1f}%",
                         f"Billed {money(li['unit_price'])} vs PO {money(pl['unit_price'])} "
                         f"(tolerance {POLICY['unit_price_tolerance_pct']:g}%). "
                         f"Extra cost {money((li['unit_price'] - pl['unit_price']) * li['qty'])}.",
                         line=pl["line_no"]))
        elif abs(var) > 0.001:
            out.append(F("PRICE_WITHIN_TOL", "info", f"Price differs {var:+.2f}% on '{pl['description']}' (within tolerance)"))
        # quantity vs receipt and previous billing
        if li["qty"] > pl["qty_billable"] + 1e-9:
            over = li["qty"] - pl["qty_billable"]
            if pl["qty_billed"] + li["qty"] > pl["qty_ordered"]:
                code, title = "QTY_OVERBILLED", f"Over-billing on '{pl['description']}'"
                det = (f"Ordered {pl['qty_ordered']:g}, already invoiced {pl['qty_billed']:g}, this invoice {li['qty']:g} "
                       f"-> {pl['qty_billed'] + li['qty']:g} total, {pl['qty_billed'] + li['qty'] - pl['qty_ordered']:g} over the PO.")
            else:
                code, title = "QTY_NOT_RECEIVED", f"Billing for goods not yet received: '{pl['description']}'"
                det = (f"Received {pl['qty_received']:g}, already invoiced {pl['qty_billed']:g}, this invoice {li['qty']:g}. "
                       f"{over:g} unit(s) not yet received.")
            out.append(F(code, "fail", title, det, line=pl["line_no"]))
            r["status"] = "qty exceeds billable"
        else:
            r["status"] = "ok" if var <= POLICY["unit_price_tolerance_pct"] else "price variance"
        allocs.append((pl["line_no"], li["qty"], li.get("amount") or li["qty"] * li["unit_price"]))
        line_results.append(r)

    ctx["line_results"] = line_results
    ctx["allocs"] = allocs
    if all(r.get("status") == "ok" for r in line_results):
        out.append(F("THREE_WAY", "pass", f"All {len(line_results)} line(s) match PO price and goods receipt",
                     f"Quantities are within what has been received and not yet billed."))

    # header tolerance (lesser of % or absolute)
    sub = inv.get("subtotal") or 0
    tol = min(expected_value * POLICY["amount_tolerance_pct"] / 100, POLICY["amount_tolerance_abs"])
    diff = sub - expected_value
    if abs(diff) <= max(tol, POLICY["rounding_slack"]):
        out.append(F("AMOUNT_TOL", "pass", "Invoice value matches PO prices",
                     f"Expected {money(expected_value)}, billed {money(sub)} (tolerance {money(tol)})."))
    else:
        out.append(F("AMOUNT_VARIANCE", "warn" if price_fail else "fail",
                     f"Invoice value off by {money(diff)} vs PO prices",
                     f"Expected {money(expected_value)}, billed {money(sub)}, tolerance {money(tol)}."))
    # cumulative PO consumption
    after = po["billed_value"] + sub
    if after > po["total"] + tol:
        out.append(F("PO_VALUE_EXCEEDED", "warn" if price_fail else "fail", f"{po['po_number']} would be over-consumed",
                     f"PO value {money(po['total'])}, already billed {money(po['billed_value'])}, "
                     f"after this invoice {money(after)}." + (" Caused by the price variance above." if price_fail else "")))
    else:
        out.append(F("PO_BALANCE", "pass", "Within remaining PO balance",
                     f"{money(po['remaining_value'])} remaining before this invoice, "
                     f"{money(po['total'] - after)} after."))
    return out


# ------------------------------------------------------------------ stage: duplicates
def check_duplicates(ctx) -> list[dict]:
    inv, v, out = ctx["inv"], ctx.get("vendor"), []
    run_id = ctx["run_id"]
    prev = db.q("SELECT id, created_at, decision FROM runs WHERE file_hash=? AND id<>? ORDER BY id DESC LIMIT 1",
                (ctx["file_hash"], run_id))
    if prev:
        out.append(F("DUP_FILE", "block", f"This exact file was already processed (run #{prev[0]['id']})",
                     f"Previous decision: {prev[0]['decision'] or 'in progress'}.", run_id=prev[0]["id"]))
    norm = norm_invoice_number(inv.get("invoice_number"))
    if v and norm:
        led = db.q("SELECT * FROM ledger WHERE vendor_id=? AND invoice_number_norm=?", (v["vendor_id"], norm))
        if led:
            l0 = led[0]
            same_lit = l0["invoice_number"] == inv["invoice_number"]
            out.append(F("DUP_INVOICE", "block",
                         f"Invoice {inv['invoice_number']} already {l0['status']}" +
                         ("" if same_lit else f" as '{l0['invoice_number']}'"),
                         f"Booked {l0['invoice_date']} for {money(l0['total'])}. "
                         + ("" if same_lit else "Number was formatted differently; matched after normalising."),
                         ledger_invoice=l0["invoice_number"], ledger_total=l0["total"], normalised=norm))
        pend = db.q("SELECT id FROM runs WHERE vendor_id=? AND invoice_number_norm=? AND id<>? "
                    "AND decision='needs_review' AND review_decision IS NULL", (v["vendor_id"], norm, run_id))
        if pend:
            out.append(F("DUP_PENDING", "block", f"Same invoice is already waiting in the review queue (run #{pend[0]['id']})"))
    # fuzzy: same vendor, same total, close dates, different number
    d = to_date(inv.get("invoice_date"))
    if v and d and inv.get("total") is not None:
        w = POLICY["fuzzy_duplicate_window_days"]
        for l in db.q("SELECT * FROM ledger WHERE vendor_id=? AND ABS(total-?)<=1", (v["vendor_id"], inv["total"])):
            ld = to_date(l["invoice_date"])
            if l["invoice_number_norm"] != norm and ld and abs((ld - d).days) <= w:
                out.append(F("POSSIBLE_DUPLICATE", "fail", f"Looks like {l['invoice_number']} (same amount, {abs((ld - d).days)} days apart)",
                             "Different invoice number, but same vendor and total within the duplicate window."))
    if not any(f["severity"] in ("block", "fail") for f in out):
        out.append(F("NO_DUPLICATE", "pass", "No duplicate found",
                     "Checked file fingerprint, normalised invoice number, and amount/date similarity."))
    return out


# ------------------------------------------------------------------ approval matrix
def approval_limit(ctx) -> list[dict]:
    t = ctx["inv"].get("total") or 0
    if t > POLICY["auto_approve_limit"]:
        return [F("ABOVE_AUTO_LIMIT", "fail", f"Above auto-approval limit ({money(POLICY['auto_approve_limit'])})",
                  "Needs Finance Controller sign-off even though checks pass.")]
    return [F("WITHIN_AUTO_LIMIT", "pass", f"Within auto-approval limit ({money(POLICY['auto_approve_limit'])})")]
