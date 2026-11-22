"""Exercise the LLM extraction path with a mocked model (no API key needed)."""
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config, extract, llm, reader

SAMPLE = Path(__file__).resolve().parent.parent / "samples/01_acme_happy_path.pdf"
GOOD = {"vendor_name": "Acme Office Supplies Pvt Ltd", "vendor_gstin": "29AABCA1234F1Z5", "buyer_gstin": "29AAACN4821K1ZB",
        "invoice_number": "INV-2026-0418", "invoice_date": "2026-11-18", "due_date": "2026-12-18", "po_number": "PO-4501",
        "currency": "INR", "line_items": [{"description": "A4 copier paper", "qty": 40, "unit_price": 1450, "amount": 58000}],
        "subtotal": 121900, "tax_lines": [{"type": "CGST", "rate": 9, "amount": 10971}], "tax_total": 21942, "total": 143842,
        "bank": {"account": "50100234567812", "ifsc": "HDFC0001234", "bank_name": "HDFC Bank"}, "notes": [],
        "confidence": {"invoice_number": 0.99, "total": 0.98}}

def run(resp):
    config.LLM_PROVIDER, config.ANTHROPIC_API_KEY = "anthropic", "test"
    def fake(prompt, images=None):
        if isinstance(resp, Exception): raise resp
        return json.dumps(resp)
    llm.complete_json = fake
    return extract.extract(reader.read(SAMPLE))

r = run(GOOD); assert r["method"] == "llm+rules" and r["disagreements"] == [], r
print("agree      ->", r["method"], r["disagreements"])
bad = {**GOOD, "total": 134842}
r = run(bad); assert r["disagreements"] and r["disagreements"][0]["field"] == "total", r
print("disagree   ->", r["disagreements"])
r = run(RuntimeError("429 rate limited")); assert r["method"] == "rules (LLM fallback)" and r["invoice"]["total"] == 143842
print("api error  ->", r["method"], "|", r["llm_error"])
print("LLM PATH OK")
