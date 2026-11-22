"""Read-only reference data (vendor master + PO book) and ledger-derived balances."""
import csv
from functools import lru_cache

from rapidfuzz import fuzz

from . import db
from .config import DATA_DIR


@lru_cache
def vendors() -> list[dict]:
    with open(DATA_DIR / "vendors.csv") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["aliases"] = [a for a in r["aliases"].split("|") if a]
        r["payment_terms_days"] = int(r["payment_terms_days"])
    return rows


@lru_cache
def purchase_orders() -> dict[str, dict]:
    with open(DATA_DIR / "purchase_orders.csv") as f:
        pos = {r["po_number"]: {**r, "lines": []} for r in csv.DictReader(f)}
    with open(DATA_DIR / "po_lines.csv") as f:
        for r in csv.DictReader(f):
            for k in ("qty_ordered", "unit_price", "tax_rate", "qty_received"):
                r[k] = float(r[k])
            r["line_no"] = int(r["line_no"])
            pos[r["po_number"]]["lines"].append(r)
    for p in pos.values():
        p["total"] = round(sum(l["qty_ordered"] * l["unit_price"] for l in p["lines"]), 2)
    return pos


def vendor_by_id(vid):
    return next((v for v in vendors() if v["vendor_id"] == vid), None)


def find_vendor(name: str | None, gstin: str | None):
    """Returns (vendor, method, score). GSTIN is the strongest key; name is fuzzy."""
    if gstin:
        for v in vendors():
            if v["gstin"] == gstin.upper():
                return v, "gstin", 100.0
    if name:
        best, best_s = None, 0.0
        for v in vendors():
            for cand in [v["name"], *v["aliases"]]:
                s = fuzz.token_set_ratio(name.lower(), cand.lower())
                if s > best_s:
                    best, best_s = v, s
        return best, "name", best_s
    return None, None, 0.0


def billed_by_line(po_number: str) -> dict[int, float]:
    rows = db.q("SELECT po_line, SUM(qty) q FROM ledger_lines WHERE po_number=? GROUP BY po_line", (po_number,))
    return {r["po_line"]: r["q"] for r in rows}


def billed_value(po_number: str) -> float:
    r = db.q("SELECT COALESCE(SUM(amount),0) v FROM ledger_lines WHERE po_number=?", (po_number,))
    return r[0]["v"]


def po_with_balances(po_number: str) -> dict | None:
    po = purchase_orders().get(po_number)
    if not po:
        return None
    billed = billed_by_line(po_number)
    lines = []
    for l in po["lines"]:
        b = billed.get(l["line_no"], 0.0)
        lines.append({**l, "qty_billed": b,
                      "qty_billable": max(0.0, min(l["qty_ordered"], l["qty_received"]) - b)})
    return {**po, "lines": lines, "billed_value": billed_value(po_number),
            "remaining_value": round(po["total"] - billed_value(po_number), 2)}
