"""Stage 2: get text out of the document. Text layer first; if it's empty or
too thin, rasterise and OCR. Also returns page images for LLM vision."""
import base64
import io

import pdfplumber
import pypdfium2 as pdfium
import pytesseract
from PIL import Image, ImageOps

MIN_CHARS = 80


def _render(path, dpi=300):
    pdf = pdfium.PdfDocument(str(path))
    imgs = [pdf[i].render(scale=dpi / 72).to_pil() for i in range(len(pdf))]
    pdf.close()
    return imgs


def read(path) -> dict:
    out = {"pages": 0, "method": None, "text": "", "chars": 0, "ocr_confidence": None,
           "images_b64": []}
    if str(path).lower().endswith((".png", ".jpg", ".jpeg")):
        images = [Image.open(path)]
        out["pages"] = 1
        text_layer = ""
    else:
        with pdfplumber.open(path) as pdf:
            out["pages"] = len(pdf.pages)
            text_layer = "\n".join((p.extract_text() or "") for p in pdf.pages)
        images = None

    if len(text_layer.strip()) >= MIN_CHARS:
        out.update(method="text_layer", text=text_layer, chars=len(text_layer))
        return out

    # OCR path
    images = images or _render(path)
    texts, confs = [], []
    for img in images:
        g = ImageOps.autocontrast(img.convert("L"))
        try:
            osd = pytesseract.image_to_osd(g, config="--psm 0 -c min_characters_to_try=5")
            angle = int([l for l in osd.splitlines() if l.startswith("Rotate:")][0].split(":")[1])
            if angle:
                g = g.rotate(-angle, expand=True, fillcolor=255)
        except Exception:
            pass
        data = pytesseract.image_to_data(g, config="--psm 6", output_type=pytesseract.Output.DICT)
        confs += [float(c) for c in data["conf"] if str(c) not in ("-1", "") and float(c) >= 0]
        texts.append(pytesseract.image_to_string(g, config="--psm 6"))
        buf = io.BytesIO()
        img.convert("RGB").resize((img.width // 2, img.height // 2)).save(buf, "JPEG", quality=80)
        out["images_b64"].append(base64.b64encode(buf.getvalue()).decode())
    text = "\n".join(texts)
    out.update(method="ocr", text=text, chars=len(text),
               ocr_confidence=round(sum(confs) / len(confs) / 100, 3) if confs else 0.0)
    return out
