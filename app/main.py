import asyncio
import json
import re
import shutil
import threading
from collections import Counter

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, pipeline, reference
from .config import ROOT, SAMPLES_DIR, STORAGE_DIR

app = FastAPI(title="Northwind AP - invoice decisioning")
UPLOADS = STORAGE_DIR / "uploads"


@app.on_event("startup")
def _startup():
    UPLOADS.mkdir(parents=True, exist_ok=True)
    db.init()


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return (ROOT / "static" / "index.html").read_text()


@app.get("/api/meta")
def meta():
    return {"stages": pipeline.STAGES, "policy": config.POLICY, "buyer": config.BUYER,
            "llm": {"enabled": config.llm_enabled(), "provider": config.LLM_PROVIDER or None,
                    "model": config.ANTHROPIC_MODEL if config.LLM_PROVIDER == "anthropic" else
                    config.OPENAI_MODEL if config.LLM_PROVIDER == "openai" else None}}


@app.get("/api/samples")
def samples():
    return json.loads((SAMPLES_DIR / "manifest.json").read_text())


@app.get("/samples/{name}")
def sample_file(name: str):
    p = (SAMPLES_DIR / name).resolve()
    if p.parent != SAMPLES_DIR.resolve() or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="application/pdf")


@app.post("/api/runs")
async def create_run(file: UploadFile | None = File(None), sample: str | None = Form(None)):
    if sample:
        src = (SAMPLES_DIR / sample).resolve()
        if src.parent != SAMPLES_DIR.resolve() or not src.exists():
            raise HTTPException(404, "Sample not found")
        name, data = sample, src.read_bytes()
    elif file:
        name = re.sub(r"[^A-Za-z0-9._-]", "_", file.filename or "invoice.pdf")[-120:]
        if not name.lower().endswith((".pdf", ".png", ".jpg", ".jpeg")):
            raise HTTPException(400, "Upload a PDF, PNG or JPG invoice")
        data = await file.read()
        if len(data) > 15 * 1024 * 1024:
            raise HTTPException(400, "File is larger than 15 MB")
    else:
        raise HTTPException(400, "Provide a file or a sample name")
    run_id = db.create_run(name, None, "")
    path = UPLOADS / f"{run_id}_{name}"
    path.write_bytes(data)
    db.update_run(run_id, file_path=str(path))
    threading.Thread(target=pipeline.run, args=(run_id, str(path), name), daemon=True).start()
    return {"run_id": run_id}


def _run_row(r):
    r = dict(r)
    r["result"] = json.loads(r.pop("result_json") or "null")
    return r


@app.get("/api/runs")
def list_runs(decision: str | None = None, q: str | None = None):
    sql, args = "SELECT * FROM runs WHERE 1=1", []
    if decision:
        sql += " AND decision=?"
        args.append(decision)
    if q:
        sql += " AND (vendor_name LIKE ? OR invoice_number LIKE ? OR po_number LIKE ? OR filename LIKE ?)"
        args += [f"%{q}%"] * 4
    rows = db.q(sql + " ORDER BY id DESC LIMIT 500", args)
    for r in rows:
        r.pop("result_json", None)
    return rows


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    rows = db.q("SELECT * FROM runs WHERE id=?", (run_id,))
    if not rows:
        raise HTTPException(404)
    r = _run_row(rows[0])
    r["events"] = db.events_after(run_id, 0)
    return r


@app.get("/api/runs/{run_id}/file")
def run_file(run_id: int):
    rows = db.q("SELECT file_path, filename FROM runs WHERE id=?", (run_id,))
    if not rows or not rows[0]["file_path"]:
        raise HTTPException(404)
    mt = "application/pdf" if rows[0]["filename"].lower().endswith(".pdf") else "image/png"
    return FileResponse(rows[0]["file_path"], media_type=mt)


@app.get("/api/runs/{run_id}/stream")
async def stream(run_id: int):
    async def gen():
        seq = 0
        idle = 0
        while True:
            evs = db.events_after(run_id, seq)
            for e in evs:
                seq = e["seq"]
                yield f"data: {json.dumps(e, default=str)}\n\n"
            st = db.q("SELECT status FROM runs WHERE id=?", (run_id,))
            if st and st[0]["status"] in ("done", "error") and not evs:
                yield "event: done\ndata: {}\n\n"
                return
            idle = 0 if evs else idle + 1
            if idle > 600:  # ~2 minutes with nothing new
                return
            await asyncio.sleep(0.2)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class Review(BaseModel):
    action: str  # approve | reject
    comment: str = ""
    reviewer: str = "AP reviewer"


@app.post("/api/runs/{run_id}/review")
def review(run_id: int, body: Review):
    rows = db.q("SELECT * FROM runs WHERE id=?", (run_id,))
    if not rows:
        raise HTTPException(404)
    r = _run_row(rows[0])
    if r["decision"] != "needs_review" or r["review_decision"]:
        raise HTTPException(409, "This invoice is not waiting for review")
    if body.action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    if body.action == "approve" and not body.comment.strip():
        raise HTTPException(400, "Add a comment explaining why you're approving an exception")
    res = r["result"] or {}
    if body.action == "approve":
        if not (res.get("po") and r["vendor_id"]):
            raise HTTPException(409, "Can't approve without a vendor and PO match")
        db.post_to_ledger(run_id, r["vendor_id"], res["invoice"], res["po"]["po_number"],
                          [tuple(a) for a in res.get("allocs", [])], status="approved")
    db.update_run(run_id, review_decision=body.action + "d", review_comment=body.comment,
                  reviewed_by=body.reviewer, reviewed_at=db.now())
    db.add_event(run_id, "review", "review", "pass" if body.action == "approve" else "block",
                 f"{'Approved' if body.action == 'approve' else 'Rejected'} by {body.reviewer}",
                 {"comment": body.comment})
    return {"ok": True}


@app.get("/api/review-queue")
def review_queue():
    rows = db.q("SELECT * FROM runs WHERE decision='needs_review' AND review_decision IS NULL ORDER BY id DESC")
    out = []
    for r in rows:
        r = _run_row(r)
        exc = (r["result"] or {}).get("stages", {}).get("decide", {}).get("data", {}).get("exceptions", [])
        r["exceptions"] = exc
        r["next_actions"] = (r["result"] or {}).get("next_actions", [])
        r.pop("result")
        out.append(r)
    return out


@app.get("/api/dashboard")
def dashboard():
    runs = db.q("SELECT * FROM runs WHERE status='done' ORDER BY id")
    n = len(runs)
    by = Counter(r["decision"] for r in runs)
    final_approved = [r for r in runs if r["decision"] == "approved" or r["review_decision"] == "approved"]
    codes = Counter()
    for r in runs:
        res = json.loads(r["result_json"] or "{}")
        for f in res.get("stages", {}).get("decide", {}).get("data", {}).get("exceptions", []):
            codes[f["code"]] += 1
    durs = [r["duration_ms"] for r in runs if r["duration_ms"]]
    return {
        "total": n, "approved": by["approved"], "needs_review": by["needs_review"], "rejected": by["rejected"],
        "open_reviews": sum(1 for r in runs if r["decision"] == "needs_review" and not r["review_decision"]),
        "touchless_rate": round(by["approved"] / n, 3) if n else 0,
        "value_processed": round(sum(r["total"] or 0 for r in runs), 2),
        "value_approved": round(sum(r["total"] or 0 for r in final_approved), 2),
        "value_held": round(sum(r["total"] or 0 for r in runs if r["decision"] == "needs_review" and not r["review_decision"]), 2),
        "value_stopped": round(sum(r["total"] or 0 for r in runs if r["decision"] == "rejected" or r["review_decision"] == "rejected"), 2),
        "avg_seconds": round(sum(durs) / len(durs) / 1000, 1) if durs else 0,
        "exceptions": codes.most_common(10),
    }


@app.get("/api/reference")
def ref():
    pos = [reference.po_with_balances(p) for p in reference.purchase_orders()]
    vendors = [{**v, "bank_account": "..." + v["bank_account"][-4:]} for v in reference.vendors()]
    return {"vendors": vendors, "pos": pos, "ledger": db.q("SELECT * FROM ledger ORDER BY id DESC")}


@app.post("/api/reset")
def reset():
    db.init(reset=True)
    shutil.rmtree(UPLOADS, ignore_errors=True)
    UPLOADS.mkdir(parents=True, exist_ok=True)
    return {"ok": True}
