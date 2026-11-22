"""GSTIN format + checksum validation (India GST identification number).

Format: 2-digit state code + 10-char PAN + entity number + 'Z' + checksum char.
The checksum is a base-36 Luhn-style mod over the first 14 characters.
"""
import re

GSTIN_RE = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b")
_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

STATE_CODES = {
    "24": "Gujarat", "27": "Maharashtra", "29": "Karnataka", "33": "Tamil Nadu",
    "07": "Delhi", "36": "Telangana", "32": "Kerala", "19": "West Bengal",
}


def checksum_char(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        factor = 1 if i % 2 == 0 else 2
        prod = _CHARS.index(ch) * factor
        total += prod // 36 + prod % 36
    return _CHARS[(36 - total % 36) % 36]


def make_gstin(state: str, pan: str, entity: str = "1") -> str:
    base = f"{state}{pan}{entity}Z"
    return base + checksum_char(base)


def validate(gstin: str | None) -> tuple[bool, str]:
    if not gstin:
        return False, "missing"
    g = gstin.strip().upper()
    if not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]", g):
        return False, "bad format"
    if g[:2] not in STATE_CODES and not (1 <= int(g[:2]) <= 38):
        return False, "unknown state code"
    if checksum_char(g[:14]) != g[14]:
        return False, "checksum mismatch"
    return True, "valid"
