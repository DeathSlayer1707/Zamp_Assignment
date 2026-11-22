"""Stage 3: turn document text into a structured invoice.

Two independent extractors:
  * rules  - deterministic, label-driven parser (always runs, no network)
  * llm    - schema-constrained LLM extraction (runs when an API key is set)

When both run, they are reconciled field by field. A disagreement on a
money field or the invoice number is surfaced as a finding, because that is
exactly where a silent extraction error turns into a wrong payment.
"""
import json
import re

from . import llm
from .config import BUYER, llm_enabled
from .gstin import GSTIN_RE
from .normalise import parse_amount, parse_date

NUM = r"[\d][\d,\.]*\d"

INV_NO_RE = re.compile(
    r"(?im)\b(?:tax\s+invoice\s+no|invoice\s*(?:no\.?|number|#)|inv\.?\s*no\.?|bill\s*(?:no\.?|number|#))"
    r"\s*[:#.]?\s*([A-Z0-9][A-Z0-9\-/]{2,})")
INV_DATE_RE = re.compile(
    r"(?im)\b(?:invoice\s*date|date\s*of\s*issue|bill\s*date|inv\.?\s*date|dated)\s*[:.]?\s*"
    r"([0-9]{1,4}[\-/. ][A-Za-z0-9]{1,9}[\-/. ,]+[0-9]{2,4})")
DUE_RE = re.compile(r"(?im)\b(?:due\s*date|payment\s*due|due\s*by|due)\s*[:.]?\s*"
                    r"([0-9]{1,4}[\-/. ][A-Za-z0-9]{1,9}[\-/. ,]+[0-9]{2,4})")
PO_RE = re.compile(r"(?i)\bP\.?\s?O\.?\s?[-#:]?\s?(\d{4,6})\b")
SUBTOTAL_RE = re.compile(rf"(?im)^.*\b(?:sub\s*-?\s*total|taxable\s*(?:value|amount))\b.*?({NUM})\s*$")
TAX_RE = re.compile(rf"(?im)^.*\b(CGST|SGST|IGST|UTGST)\b\s*@?\s*(\d{{1,2}}(?:\.\d+)?)\s*%.*?({NUM})\s*$")
TOTAL_RE = re.compile(rf"(?im)^\s*(?:grand\s*total|total\s*payable|amount\s*due|invoice\s*total|total\s*amount|total)\b[^\n]*?({NUM})\s*$")
LINE_RE = re.compile(rf"(?m)^\s*(?:\d{{1,2}}[.)]?\s+)?(?P<desc>[A-Za-z].*?)\s+(?P<qty>\d+(?:\.\d+)?)\s+(?P<rate>{NUM})\s+(?P<amt>{NUM})\s*$")
ACCT_RE = re.compile(r"(?i)\b(?:a/?c|account)\s*(?:no\.?|number|#)?\s*[:.]?\s*(\d{9,18})\b")
IFSC_RE = re.compile(r"\b([A-Z]{4}0[A-Z0-9]{6})\b")
BANK_RE = re.compile(r"(?im)^\s*bank(?:\s*name)?\s*[:.]\s*(.+?)\s*$")
NOTE_KEYWORDS = re.compile(r"(?i)(bank details|new account|changed|kindly remit|urgent|reminder|revised|credit note)")
HEADER_NOTE = re.compile(r"(?i)^\s*bank details for remittance\s*$")
TITLE_WORDS = re.compile(r"(?i)\s*(tax\s+invoice|invoice|bill)\s*$")
SKIP_LINE = re.compile(r"(?i)(sub\s*total|taxable|cgst|sgst|igst|total|amount due)")


def _first(rx, text, group=1):
    m = rx.search(text)
    return m.group(group).strip() if m else None


def extract_rules(text: str, ocr: bool) -> dict:
    base_conf = 0.85 if ocr else 0.97
    lines_txt = [l for l in text.splitlines() if l.strip()]
    inv = {"currency": "INR", "line_items": [], "tax_lines": [], "notes": [], "_confidence": {}}
    conf = inv["_confidence"]

    # vendor name = first line minus document title
    if lines_txt:
        inv["vendor_name"] = TITLE_WORDS.sub("", lines_txt[0]).strip(" :-|") or None
        conf["vendor_name"] = base_conf * 0.9 if inv["vendor_name"] else 0
    gstins = [g for g in GSTIN_RE.findall(text.upper())]
    inv["vendor_gstin"] = next((g for g in gstins if g != BUYER["gstin"]), None)
    inv["buyer_gstin"] = BUYER["gstin"] if BUYER["gstin"] in gstins else None
    conf["vendor_gstin"] = base_conf if inv["vendor_gstin"] else 0

    inv["invoice_number"] = _first(INV_NO_RE, text)
    conf["invoice_number"] = base_conf if inv["invoice_number"] else 0
    raw_date = _first(INV_DATE_RE, text)
    inv["invoice_date"] = parse_date(raw_date)
    conf["invoice_date"] = base_conf if inv["invoice_date"] else 0
    inv["due_date"] = parse_date(_first(DUE_RE, text))
    po = PO_RE.search(text)
    inv["po_number"] = f"PO-{po.group(1)}" if po else None
    conf["po_number"] = base_conf if po else 0

    for m in LINE_RE.finditer(text):
        desc = m.group("desc").strip()
        if SKIP_LINE.search(desc):
            continue
        qty, rate, amt = float(m.group("qty")), parse_amount(m.group("rate")), parse_amount(m.group("amt"))
        if rate is None or amt is None:
            continue
        inv["line_items"].append({"description": desc, "qty": qty, "unit_price": rate, "amount": amt})
    conf["line_items"] = base_conf if inv["line_items"] else 0

    inv["subtotal"] = parse_amount(_first(SUBTOTAL_RE, text))
    for m in TAX_RE.finditer(text):
        inv["tax_lines"].append({"type": m.group(1).upper(), "rate": float(m.group(2)),
                                 "amount": parse_amount(m.group(3))})
    inv["tax_total"] = round(sum(t["amount"] or 0 for t in inv["tax_lines"]), 2) if inv["tax_lines"] else None
    total = None
    for m in TOTAL_RE.finditer(text):
        if not SUBTOTAL_RE.match(m.group(0)):
            total = parse_amount(m.group(1))
    inv["total"] = total
    conf["total"] = base_conf if total is not None else 0
    conf["subtotal"] = base_conf if inv["subtotal"] is not None else 0

    inv["bank"] = {"account": _first(ACCT_RE, text), "ifsc": None, "bank_name": _first(BANK_RE, text)}
    ifscs = IFSC_RE.findall(text)
    inv["bank"]["ifsc"] = ifscs[0] if ifscs else None
    inv["notes"] = [l.strip() for l in lines_txt
                  if NOTE_KEYWORDS.search(l) and not BANK_RE.match(l) and not HEADER_NOTE.match(l)]
    inv["_source"] = "rules"
    return inv


LLM_SCHEMA_PROMPT = """You are an accounts-payable extraction engine. Extract the invoice below into JSON.
Rules:
- Return ONLY a JSON object, no prose.
- Use null for anything not present. Never guess or compute a value that is not printed,
  except tax_total which may be the sum of printed tax lines.
- Dates as YYYY-MM-DD (Indian documents are day-first).
- Amounts as plain numbers (no commas, no currency symbols).
- po_number: only if a purchase order reference is printed.
- notes: any free-text remarks to the payer (e.g. bank change notices, reminders).
- confidence: your 0-1 confidence for each top-level field.

Schema:
{"vendor_name": str, "vendor_gstin": str, "buyer_gstin": str, "invoice_number": str,
 "invoice_date": str, "due_date": str, "po_number": str, "currency": str,
 "line_items": [{"description": str, "qty": number, "unit_price": number, "amount": number}],
 "subtotal": number, "tax_lines": [{"type": "CGST|SGST|IGST|UTGST", "rate": number, "amount": number}],
 "tax_total": number, "total": number,
 "bank": {"account": str, "ifsc": str, "bank_name": str},
 "notes": [str], "confidence": {"<field>": number}}
"""


def extract_llm(text: str, images_b64: list[str]) -> dict:
    raw = llm.complete_json(LLM_SCHEMA_PROMPT + "\n\nDOCUMENT TEXT:\n" + text[:15000], images_b64)
    inv = json.loads(raw)
    inv["_confidence"] = inv.pop("confidence", {}) or {}
    inv["_source"] = "llm"
    inv.setdefault("line_items", [])
    inv.setdefault("tax_lines", [])
    inv.setdefault("notes", [])
    inv.setdefault("bank", {})
    for k in ("subtotal", "tax_total", "total"):
        inv[k] = parse_amount(inv.get(k))
    for li in inv["line_items"]:
        for k in ("qty", "unit_price", "amount"):
            li[k] = parse_amount(li.get(k))
    inv["invoice_date"] = parse_date(inv.get("invoice_date")) or inv.get("invoice_date")
    inv["due_date"] = parse_date(inv.get("due_date")) or inv.get("due_date")
    return inv


KEY_FIELDS = ["invoice_number", "invoice_date", "po_number", "vendor_gstin", "subtotal", "total"]


def reconcile(primary: dict, secondary: dict) -> list[dict]:
    """Compare two extractions; return list of disagreements."""
    diffs = []
    for k in KEY_FIELDS:
        a, b = primary.get(k), secondary.get(k)
        if a is None or b is None:
            continue
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if abs(a - b) > 1:
                diffs.append({"field": k, "llm": a, "rules": b})
        elif str(a).strip().upper() != str(b).strip().upper():
            diffs.append({"field": k, "llm": a, "rules": b})
    return diffs


def extract(doc: dict) -> dict:
    """Returns {invoice, method, disagreements, llm_error}."""
    ocr = doc["method"] == "ocr"
    rules = extract_rules(doc["text"], ocr)
    result = {"invoice": rules, "method": "rules", "disagreements": [], "llm_error": None}
    if llm_enabled():
        try:
            li = extract_llm(doc["text"], doc.get("images_b64") if ocr else [])
            # fill any LLM gaps from rules, keep provenance
            for k, v in rules.items():
                if li.get(k) in (None, [], {}) and v not in (None, [], {}):
                    li[k] = v
            result.update(invoice=li, method="llm+rules", disagreements=reconcile(li, rules))
        except Exception as e:  # network, quota, bad JSON -> deterministic fallback
            result["llm_error"] = f"{type(e).__name__}: {e}"[:300]
            result["method"] = "rules (LLM fallback)"
    if ocr and doc.get("ocr_confidence") is not None:
        c = result["invoice"]["_confidence"]
        for k in list(c):
            c[k] = round(min(c[k], max(doc["ocr_confidence"], 0.5) + 0.1), 3)
    return result
