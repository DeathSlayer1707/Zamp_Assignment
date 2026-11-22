import re
from datetime import date, datetime

DATE_FORMATS = ["%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
                "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y", "%d %b, %Y", "%d/%m/%y"]


def parse_date(s: str | None) -> str | None:
    """Return ISO date. Ambiguous numeric dates are read day-first (Indian convention)."""
    if not s:
        return None
    s = re.sub(r"\s+", " ", s.strip().rstrip(".,"))
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def to_date(iso: str | None) -> date | None:
    return date.fromisoformat(iso) if iso else None


def parse_amount(s) -> float | None:
    """Handles Indian grouping (1,43,842.00), Rs/INR prefixes and common OCR slips
    such as '1.450.00' (comma read as a full stop)."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = re.sub(r"(?i)(rs\.?|inr|₹)", "", str(s)).strip().replace(" ", "")
    t = t.replace(",", "")
    if t.count(".") > 1:
        head, _, tail = t.rpartition(".")
        t = head.replace(".", "") + "." + tail
    try:
        return float(t)
    except ValueError:
        return None


def norm_invoice_number(s: str | None) -> str | None:
    """'SL/7781', 'SL-7781', 'sl 07781' -> 'SL7781'. Used for duplicate detection."""
    if not s:
        return None
    u = s.upper()
    u = re.sub(r"(?<![0-9])0+(?=[0-9])", "", u)
    return re.sub(r"[^A-Z0-9]", "", u) or None
