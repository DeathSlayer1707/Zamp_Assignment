# Invoice desk: PDF invoice to a reasoned AP decision

Zamp ASA case study, PS-1 (Finance / AP).

You drop in a vendor invoice (clean PDF, scanned image or photo). It goes through nine checks you can watch live, and comes out as one of three decisions:

- **Approved**: posted to the ledger and scheduled for payment
- **Needs review**: routed to the right team (Procurement, Treasury, Tax, AP), with a drafted message
- **Rejected**: not payable as sent, with a drafted message to the vendor

Every decision lists its reasons, and every step is kept in an audit trail.

```
invoice ─► 1 Receive ─► 2 Read (text layer / OCR) ─► 3 Extract (LLM + rules) ─► 4 Validate
        ─► 5 Vendor ─► 6 PO + goods receipt (3-way) ─► 7 GST ─► 8 Duplicates ─► 9 Decide + route
```

The core design rule: **the LLM only reads the document; deterministic rules make the decision.**

## Run it locally

```bash
# needs Python 3.11+, plus tesseract and poppler for scanned invoices
#   macOS:  brew install tesseract poppler
#   Ubuntu: sudo apt install tesseract-ocr poppler-utils
pip install -r requirements.txt
python scripts/generate_samples.py          # (re)build the 9 test invoices
uvicorn app.main:app --reload               # open http://localhost:8000
```

Or with Docker: `docker build -t invoice-desk . && docker run -p 8000:8000 invoice-desk`

### Optional: LLM extraction

With no key it uses the built-in rules parser, which handles all the samples. Add a key to get LLM extraction, cross-checked field by field against the rules parser:

```bash
export LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant-...
# or any OpenAI-compatible API, e.g. Gemini's free tier:
export LLM_PROVIDER=openai OPENAI_API_KEY=... \
       OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai OPENAI_MODEL=gemini-2.5-flash
```

If the LLM call fails (quota, timeout, bad JSON), the run falls back to the rules parser and records that in a finding.

## Deploy (to get a live link)

**Render** (free): push this folder to GitHub, then New > Blueprint > pick the repo. It reads `render.yaml` and builds the Dockerfile. Add API keys under Environment.
**Hugging Face Spaces** works too: create a Docker Space, push the repo, and set `PORT=7860` as a variable.

Free hosts wipe the SQLite file on restart. That's fine for a demo, and the **Reset demo data** button (POs and vendors page) restores the starting state before you record.

## Tests

```bash
python tests/run_all.py        # every sample in demo order, asserts the expected decision
python tests/test_llm_path.py  # LLM path with a mocked model: agree / disagree / API failure
```

## Layout

| Path | What's there |
|---|---|
| `app/pipeline.py` | Orchestrator: stages, decision, routing, drafted messages |
| `app/checks.py` | All AP controls (validation, vendor, 3-way match, GST, duplicates) |
| `app/extract.py`, `app/llm.py` | Rules parser, LLM extraction, reconciliation |
| `app/reader.py` | PDF text layer, with OCR fallback (tesseract) |
| `app/config.py` | Policy knobs: tolerances, approval limit, thresholds |
| `data/*.csv` | Vendor master, PO book with goods receipts, invoice history |
| `samples/` | 9 test invoices plus `manifest.json` |
| `static/` | UI: live run view, dashboard, review queue, reference data |
| `docs/DESIGN.md` | Design, assumptions, edge cases, trade-offs, demo script |
