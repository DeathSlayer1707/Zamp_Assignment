"""Policy knobs. Everything the AP controller might want to tune lives here,
so a rule change is a config change, not a code change."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SAMPLES_DIR = ROOT / "samples"
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", ROOT / "storage"))
DB_PATH = STORAGE_DIR / "ap.db"

BUYER = {
    "name": "Northwind Retail Pvt Ltd",
    "gstin": "29AAACN4821K1ZB",
    "state_code": "29",
}

POLICY = {
    # Header-level price tolerance: auto-approve if invoice value is within
    # the LOWER of these two (classic AP "lesser of % or absolute" rule).
    "amount_tolerance_pct": 1.0,
    "amount_tolerance_abs": 500.0,
    # Per-line unit price tolerance
    "unit_price_tolerance_pct": 1.0,
    # Rounding slack for arithmetic checks (paise/rupee rounding)
    "rounding_slack": 1.0,
    # Invoices above this need a human approver even when every check passes
    "auto_approve_limit": 500000.0,
    # Date sanity
    "max_invoice_age_days": 180,
    # Fuzzy duplicate window (same vendor + same total, different number)
    "fuzzy_duplicate_window_days": 15,
    # If PO had to be inferred (no reference on invoice) – auto approve or hold?
    "auto_approve_inferred_po": False,
    # Below this overall extraction confidence, hold for a human
    "min_extraction_confidence": 0.80,
    # Fuzzy matching thresholds (0-100)
    "vendor_name_match": 85,
    "line_desc_match": 60,
}

# Demo pacing: small pause between stages so the live view is readable.
STAGE_DELAY_MS = int(os.getenv("STAGE_DELAY_MS", "450"))

# LLM extraction (optional). Without a key, the deterministic parser is used.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "").lower()  # "anthropic" | "openai" | ""
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")


def llm_enabled() -> bool:
    if LLM_PROVIDER == "anthropic":
        return bool(ANTHROPIC_API_KEY)
    if LLM_PROVIDER == "openai":
        return bool(OPENAI_API_KEY)
    return False
