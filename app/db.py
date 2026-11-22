"""SQLite persistence: runs, stage events (audit trail) and the AP ledger
(invoices already approved/paid, with per-PO-line quantities)."""
import csv
import json
import sqlite3
import threading
from datetime import datetime, timezone

from .config import DATA_DIR, DB_PATH, STORAGE_DIR

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT, finished_at TEXT, filename TEXT, file_hash TEXT, file_path TEXT,
  status TEXT, decision TEXT, queue TEXT, vendor_id TEXT, vendor_name TEXT,
  invoice_number TEXT, invoice_number_norm TEXT, invoice_date TEXT, po_number TEXT,
  total REAL, currency TEXT, confidence REAL, summary TEXT, extraction_method TEXT,
  duration_ms INTEGER, result_json TEXT,
  review_decision TEXT, review_comment TEXT, reviewed_by TEXT, reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, seq INTEGER, ts TEXT,
  stage TEXT, kind TEXT, status TEXT, title TEXT, data_json TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_id TEXT, invoice_number TEXT,
  invoice_number_norm TEXT, invoice_date TEXT, po_number TEXT, subtotal REAL,
  total REAL, status TEXT, source TEXT, run_id INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS ledger_lines (
  ledger_id INTEGER, po_number TEXT, po_line INTEGER, qty REAL, amount REAL
);
CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id, seq);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def conn() -> sqlite3.Connection:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init(reset: bool = False):
    from .normalise import norm_invoice_number

    with _lock, conn() as c:
        if reset:
            c.executescript("DROP TABLE IF EXISTS runs; DROP TABLE IF EXISTS events;"
                            "DROP TABLE IF EXISTS ledger; DROP TABLE IF EXISTS ledger_lines;")
        c.executescript(SCHEMA)
        if c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0:
            # seed with invoice history
            by_inv = {}
            with open(DATA_DIR / "invoice_history.csv") as f:
                for r in csv.DictReader(f):
                    by_inv.setdefault((r["vendor_id"], r["invoice_number"]), []).append(r)
            for (vid, num), rows in by_inv.items():
                r0 = rows[0]
                cur = c.execute(
                    "INSERT INTO ledger(vendor_id,invoice_number,invoice_number_norm,invoice_date,"
                    "po_number,subtotal,total,status,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (vid, num, norm_invoice_number(num), r0["invoice_date"], r0["po_number"],
                     float(r0["subtotal"]), float(r0["total"]), r0["status"], "history", now()))
                for r in rows:
                    c.execute("INSERT INTO ledger_lines VALUES(?,?,?,?,?)",
                              (cur.lastrowid, r["po_number"], int(r["po_line"]),
                               float(r["qty"]), float(r["line_amount"])))


def q(sql, args=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def exec_(sql, args=()):
    with _lock, conn() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def create_run(filename, file_hash, file_path) -> int:
    return exec_("INSERT INTO runs(created_at,filename,file_hash,file_path,status) VALUES(?,?,?,?,?)",
                 (now(), filename, file_hash, file_path, "running"))


def update_run(run_id, **fields):
    if not fields:
        return
    cols = ",".join(f"{k}=?" for k in fields)
    exec_(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))


def add_event(run_id, stage, kind, status, title, data=None):
    with _lock, conn() as c:
        seq = c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
        c.execute("INSERT INTO events(run_id,seq,ts,stage,kind,status,title,data_json) VALUES(?,?,?,?,?,?,?,?)",
                  (run_id, seq, now(), stage, kind, status, title, json.dumps(data or {}, default=str)))


def events_after(run_id, seq):
    rows = q("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq", (run_id, seq))
    for r in rows:
        r["data"] = json.loads(r.pop("data_json") or "{}")
    return rows


def post_to_ledger(run_id, vendor_id, inv, po_number, line_allocs, status="approved"):
    """line_allocs: list of (po_line, qty, amount)."""
    from .normalise import norm_invoice_number

    with _lock, conn() as c:
        cur = c.execute(
            "INSERT INTO ledger(vendor_id,invoice_number,invoice_number_norm,invoice_date,po_number,"
            "subtotal,total,status,source,run_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (vendor_id, inv.get("invoice_number"), norm_invoice_number(inv.get("invoice_number")),
             inv.get("invoice_date"), po_number, inv.get("subtotal"), inv.get("total"),
             status, "run", run_id, now()))
        for po_line, qty, amt in line_allocs:
            c.execute("INSERT INTO ledger_lines VALUES(?,?,?,?,?)", (cur.lastrowid, po_number, po_line, qty, amt))
