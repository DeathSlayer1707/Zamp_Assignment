"""Generate realistic test invoices. Each vendor has its own layout, labels and
date format, the way real AP inboxes look. One invoice is rasterised and
noised to simulate a phone/scanner capture (no text layer)."""
import io
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageFilter
from reportlab.lib.colors import HexColor, black
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples"
OUT.mkdir(exist_ok=True)

BUYER = ("Northwind Retail Pvt Ltd", "No. 14, Residency Road, Bengaluru 560025", "29AAACN4821K1ZB")


def inr(x: float) -> str:
    """Indian digit grouping: 1,43,842.00"""
    neg = x < 0
    x = abs(round(x, 2))
    whole, frac = f"{x:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if neg else "") + whole + "." + frac


def draw_invoice(path, *, style, vendor, meta, lines, tax, notes=None, bank=None, accent="#1F3A5F"):
    """style: dict of label names. meta: dict of header values (missing -> omitted)."""
    c = canvas.Canvas(str(path), pagesize=A4)
    W, H = A4
    ac = HexColor(accent)
    y = H - 50
    # Vendor block (left)
    c.setFillColor(ac)
    c.setFont("Helvetica-Bold", 17)
    c.drawString(40, y, vendor["name"])
    c.setFillColor(black)
    c.setFont("Helvetica", 9)
    for i, ln in enumerate(vendor["address"]):
        c.drawString(40, y - 16 - i * 12, ln)
    c.drawString(40, y - 16 - len(vendor["address"]) * 12, f"GSTIN: {vendor['gstin']}")
    # Title (right)
    c.setFont("Helvetica-Bold", 14)
    c.drawRightString(W - 40, y, style["title"])

    # Meta block
    y2 = H - 150
    c.setFont("Helvetica", 10)
    for key in ("number", "date", "po", "due"):
        if meta.get(key):
            c.setFont("Helvetica-Bold", 10)
            c.drawString(330, y2, f"{style[key]}:")
            c.setFont("Helvetica", 10)
            c.drawString(440, y2, meta[key])
            y2 -= 15

    # Bill to
    yb = H - 150
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, yb, style.get("billto", "Bill To"))
    c.setFont("Helvetica", 9)
    c.drawString(40, yb - 14, BUYER[0])
    c.drawString(40, yb - 26, BUYER[1])
    c.drawString(40, yb - 38, f"GSTIN: {BUYER[2]}")

    # Table
    ty = H - 245
    c.setFillColor(ac)
    c.rect(40, ty - 4, W - 80, 18, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 9)
    cols = style["cols"]
    c.drawString(46, ty + 1, cols[0])
    c.drawRightString(395, ty + 1, cols[1])
    c.drawRightString(470, ty + 1, cols[2])
    c.drawRightString(W - 46, ty + 1, cols[3])
    c.setFillColor(black)
    c.setFont("Helvetica", 9)
    ry = ty - 20
    subtotal = 0.0
    for i, (desc, qty, rate) in enumerate(lines, 1):
        amt = round(qty * rate, 2)
        subtotal += amt
        label = f"{i} {desc}" if style.get("numbered") else desc
        c.drawString(46, ry, label)
        c.drawRightString(395, ry, f"{qty:g}")
        c.drawRightString(470, ry, inr(rate))
        c.drawRightString(W - 46, ry, inr(amt))
        ry -= 16
    c.setStrokeColor(HexColor("#999999"))
    c.line(40, ry + 6, W - 40, ry + 6)

    # Totals
    ry -= 12
    def tot(label, val, bold=False):
        nonlocal ry
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 10 if bold else 9)
        c.drawString(330, ry, label)
        c.drawRightString(W - 46, ry, inr(val))
        ry -= 15

    tot(style["subtotal"], subtotal)
    tax_total = 0.0
    for tlabel, rate in tax:
        tv = round(subtotal * rate / 100, 2)
        tax_total += tv
        tot(f"{tlabel} @ {rate:g}%", tv)
    total = round(subtotal + tax_total, 2)
    if meta.get("show_total", True):
        tot(style["total"], total, bold=True)

    # Bank + notes
    ry -= 20
    if bank:
        c.setFont("Helvetica-Bold", 9)
        c.drawString(40, ry, "Bank details for remittance")
        c.setFont("Helvetica", 9)
        c.drawString(40, ry - 13, f"Bank: {bank['bank']}")
        c.drawString(40, ry - 26, f"Account No: {bank['account']}")
        c.drawString(40, ry - 39, f"IFSC: {bank['ifsc']}")
        ry -= 58
    if notes:
        c.setFont("Helvetica-Oblique", 9)
        for n in notes:
            c.drawString(40, ry, n)
            ry -= 13
    c.setFont("Helvetica", 7)
    c.setFillColor(HexColor("#777777"))
    c.drawString(40, 40, "This is a computer generated invoice.")
    c.save()
    return round(subtotal, 2), round(total, 2)


def rasterise(src: Path, dst: Path, rotate=0.8, seed=7):
    """Turn a PDF into an image-only 'scan': grey, slightly rotated, noisy."""
    random.seed(seed)
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["pdftoppm", "-r", "170", "-gray", "-png", str(src), f"{td}/p"], check=True)
        img = Image.open(next(Path(td).glob("p*.png"))).convert("L")
    img = img.rotate(rotate, expand=True, fillcolor=245, resample=Image.BICUBIC)
    px = img.load()
    w, h = img.size
    for _ in range(int(w * h * 0.004)):
        x, y = random.randrange(w), random.randrange(h)
        px[x, y] = random.randint(120, 200)
    img = img.filter(ImageFilter.GaussianBlur(0.5))
    img = img.point(lambda v: min(255, int(v * 0.96 + 8)))  # paper tint
    img.save(dst, "PDF", resolution=170)


# ---------------------------------------------------------------- vendors
ACME = dict(name="Acme Office Supplies Pvt Ltd", gstin="29AABCA1234F1Z5",
            address=["42, 2nd Cross, Peenya Industrial Area", "Bengaluru, Karnataka 560058"])
BLUESTAR = dict(name="Bluestar IT Services LLP", gstin="27AAKFB5678L1ZS",
                address=["Office 7B, Baner Road", "Pune, Maharashtra 411045"])
GREENLEAF = dict(name="Greenleaf Facility Management Pvt Ltd", gstin="29AADCG9012M1ZA",
                 address=["118, HAL 2nd Stage, Indiranagar", "Bengaluru, Karnataka 560038"])
SWIFT = dict(name="Swift Logistics Pvt Ltd", gstin="33AAECS3456N1Z2",
             address=["Plot 9, SIDCO Industrial Estate, Guindy", "Chennai, Tamil Nadu 600032"])
ORION = dict(name="Orion Packaging Industries", gstin="24AAFFO7890P1ZG",
             address=["Shed 21, Naroda GIDC", "Ahmedabad, Gujarat 382330"])

ACME_STYLE = dict(title="TAX INVOICE", number="Invoice No", date="Invoice Date", po="PO Number",
                  due="Due Date", cols=["Description", "Qty", "Rate", "Amount"],
                  subtotal="Sub Total", total="Grand Total", numbered=True)
BLUESTAR_STYLE = dict(title="INVOICE", number="Invoice #", date="Date of Issue", po="Your Order Ref",
                      due="Payment Due", cols=["Item", "Qty", "Unit Price", "Line Total"],
                      subtotal="Taxable Value", total="Total Payable")
GREENLEAF_STYLE = dict(title="BILL", number="Bill No", date="Bill Date", po="PO", due="Due",
                       cols=["Particulars", "Qty", "Rate", "Amount"],
                       subtotal="Subtotal", total="Amount Due")
SWIFT_STYLE = dict(title="TAX INVOICE", number="Invoice Number", date="Dated", po="P.O. Ref",
                   due="Due By", cols=["Service Description", "Trips", "Rate", "Amount"],
                   subtotal="Taxable Amount", total="Invoice Total")
ORION_STYLE = dict(title="TAX INVOICE", number="Inv No", date="Inv Date", po="Order No", due="Due",
                   cols=["Description", "Qty", "Rate", "Value"], subtotal="Sub Total", total="Total")

ACME_BANK = dict(bank="HDFC Bank", account="50100234567812", ifsc="HDFC0001234")
SWIFT_BANK = dict(bank="Axis Bank", account="917020012345678", ifsc="UTIB0000456")


def main():
    manifest = []

    def add(fname, title, scenario, expect, **kw):
        draw_invoice(OUT / fname, **kw)
        manifest.append(dict(file=fname, title=title, scenario=scenario, expect=expect))

    add("01_acme_happy_path.pdf", "Acme Office Supplies - clean invoice",
        "Happy path: clean PDF, explicit PO, quantities and prices match PO and goods receipt.",
        "approved",
        style=ACME_STYLE, vendor=ACME, bank=ACME_BANK,
        meta=dict(number="INV-2026-0418", date="18-Nov-2026", po="PO-4501", due="18-Dec-2026"),
        lines=[("A4 copier paper 75gsm (box of 5 reams)", 40, 1450),
               ("Ergonomic mesh office chair", 6, 8900),
               ("Whiteboard markers (pack of 10)", 25, 420)],
        tax=[("CGST", 9), ("SGST", 9)])

    # Scanned Greenleaf invoice with no PO reference -> rasterised
    tmp = OUT / "_greenleaf_src.pdf"
    draw_invoice(tmp, style=GREENLEAF_STYLE, vendor=GREENLEAF, accent="#2F5D3A",
                 meta=dict(number="GFM-1189", date="15/11/2026"),
                 lines=[("Housekeeping services - November 2026", 1, 84000),
                        ("Pest control treatment (quarterly)", 1, 12500)],
                 tax=[("CGST", 9), ("SGST", 9)],
                 notes=["Thank you for your business."])
    rasterise(tmp, OUT / "02_greenleaf_scanned_no_po.pdf")
    tmp.unlink()
    manifest.append(dict(file="02_greenleaf_scanned_no_po.pdf",
                         title="Greenleaf FM - scanned, no PO number",
                         scenario="Edge case: image-only scan (needs OCR) and no PO reference. The process must infer the PO from vendor, lines and amount.",
                         expect="needs_review"))

    add("03_bluestar_split_invoice_2.pdf", "Bluestar IT - 2nd partial invoice on PO-4503",
        "Edge case: vendor splits one PO across invoices. Invoice 1 (12 units) is already paid; this is invoice 2 (12 more). Cumulative 24 of 30 received.",
        "approved",
        style=BLUESTAR_STYLE, vendor=BLUESTAR, accent="#0E5A8A",
        meta=dict(number="BST/26-27/0117", date="2026-11-12", po="PO-4503", due="2026-12-27"),
        lines=[("Managed 24-port network switch Cisco CBS350", 12, 18000),
               ("Switch installation and configuration", 12, 1500)],
        tax=[("IGST", 18)])

    add("04_bluestar_split_invoice_3_overbill.pdf", "Bluestar IT - 3rd invoice that over-bills PO-4503",
        "Edge case: 3rd partial invoice for 10 units. After invoices 1+2 (24 units), this takes the PO to 34 of 30 ordered/received.",
        "needs_review",
        style=BLUESTAR_STYLE, vendor=BLUESTAR, accent="#0E5A8A",
        meta=dict(number="BST/26-27/0124", date="2026-11-18", po="PO-4503", due="2027-01-02"),
        lines=[("Managed 24-port network switch Cisco CBS350", 10, 18000),
               ("Switch installation and configuration", 10, 1500)],
        tax=[("IGST", 18)])

    add("05_swift_resubmitted_duplicate.pdf", "Swift Logistics - re-sent October invoice",
        "Edge case: invoice SL-7781 was paid last month. Vendor re-sends it as 'SL/7781' with a new layout, so the file hash and the literal number both differ.",
        "rejected",
        style=SWIFT_STYLE, vendor=SWIFT, bank=SWIFT_BANK, accent="#7A3E00",
        meta=dict(number="SL/7781", date="28 October 2026", po="PO-4507", due="12 November 2026"),
        lines=[("Line-haul freight Bengaluru-Hyderabad full truck load", 5, 19500)],
        tax=[("IGST", 12)],
        notes=["Reminder: payment pending. Please process at the earliest."])

    add("06_swift_bank_details_changed.pdf", "Swift Logistics - new bank account on invoice",
        "Edge case: everything matches the PO, but the remit-to bank account differs from the vendor master. Classic invoice-redirection fraud pattern.",
        "needs_review",
        style=SWIFT_STYLE, vendor=SWIFT, accent="#7A3E00",
        bank=dict(bank="Kotak Mahindra Bank", account="4512887766", ifsc="KKBK0008123"),
        meta=dict(number="SL-8042", date="17 November 2026", po="PO-4506", due="02 December 2026"),
        lines=[("Line-haul freight Bengaluru-Chennai full truck load", 8, 21000),
               ("Loading and unloading charges", 8, 1800)],
        tax=[("IGST", 12)],
        notes=["Please note our bank details have changed. Kindly remit to the new account above."])

    add("07_acme_price_variance.pdf", "Acme Office Supplies - toner price above PO",
        "Extra: toner billed at ₹5,760 against PO price ₹5,600 (+2.9%), outside the 1% tolerance.",
        "needs_review",
        style=ACME_STYLE, vendor=ACME, bank=ACME_BANK,
        meta=dict(number="INV-2026-0431", date="19-Nov-2026", po="PO-4502", due="19-Dec-2026"),
        lines=[("Laser printer toner cartridge HP 26A", 20, 5760),
               ("Heavy duty stapler", 15, 650)],
        tax=[("CGST", 9), ("SGST", 9)])

    add("08_orion_blocked_vendor.pdf", "Orion Packaging - blocked vendor",
        "Extra: valid-looking invoice and PO, but the vendor is blocked in the vendor master.",
        "rejected",
        style=ORION_STYLE, vendor=ORION, accent="#5B2A86",
        meta=dict(number="OPI/2026/552", date="2026-11-10", po="PO-4508"),
        lines=[("Corrugated shipper box 5-ply", 500, 38)],
        tax=[("IGST", 18)])

    add("09_acme_missing_number_and_date.pdf", "Acme Office Supplies - no invoice number or date",
        "Extra: invoice is missing its number and date. Not a valid GST tax invoice, so it must be re-issued.",
        "rejected",
        style=ACME_STYLE, vendor=ACME, bank=ACME_BANK,
        meta=dict(po="PO-4509"),
        lines=[("Pantry supplies - tea coffee and snacks", 1, 18500)],
        tax=[("CGST", 9), ("SGST", 9)])

    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"wrote {len(manifest)} samples to {OUT}")


if __name__ == "__main__":
    sys.exit(main())
