"""Deterministic fixtures for multimodal tests, generated at test time (no binary files in Git)."""
import zipfile
from pathlib import Path


def font(size=28):
    from PIL import ImageFont
    return ImageFont.load_default(size=size)


def text_image(path: Path, lines, size=(900, 260), background="white", color="black") -> Path:
    from PIL import Image, ImageDraw
    image = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((30, 30 + index * 60), line, fill=color, font=font(34))
    image.save(path)
    return path


def ui_image(path: Path, button_color="#d32f2f", label="Submit", banner=True) -> Path:
    """A controlled UI screenshot: a header bar, an error banner and one button."""
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (800, 500), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 800, 70), fill="#1e3a8a")
    draw.text((24, 18), "Dashboard", fill="white", font=font(30))
    if banner:
        draw.rectangle((40, 110, 760, 180), fill="#fde2e2", outline="#b91c1c", width=3)
        draw.text((60, 128), "Error: payment failed", fill="#b91c1c", font=font(28))
    draw.rounded_rectangle((300, 300, 500, 370), radius=12, fill=button_color)
    draw.text((345, 318), label, fill="white", font=font(28))
    image.save(path)
    return path


def text_pdf(path: Path, pages) -> Path:
    """A minimal valid PDF with real embedded text (Helvetica), one string per page."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        lines = text.split("\n")
        stream = "BT /F1 14 Tf 72 720 Td 18 TL " + " ".join(
            f"({line.replace(chr(92), chr(92) * 2).replace('(', chr(92) + '(').replace(')', chr(92) + ')')}) '"
            for line in lines) + " ET"
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        content = len(objects)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content} 0 R "
                       "/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(len(objects))
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(f'{kid} 0 R' for kid in kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)
    return path


def scanned_pdf(path: Path, lines) -> Path:
    """An image-only PDF, like a scan: no embedded text at all."""
    from PIL import Image
    image_path = path.with_suffix(".scan.png")
    text_image(image_path, lines, size=(1240, 600))
    Image.open(image_path).convert("RGB").save(path, "PDF", resolution=150)
    return path


DOCX_TYPES = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
              'content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')


def docx(path: Path, paragraphs, table=None, heading="Requirements", macros=False) -> Path:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = (f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>{heading}</w:t></w:r></w:p>'
            + "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs))
    if table:
        body += "<w:tbl>" + "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>"
                                                         for cell in row) + "</w:tr>" for row in table) + "</w:tbl>"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", DOCX_TYPES)
        archive.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {w}><w:body>{body}</w:body></w:document>')
        if macros:
            archive.writestr("word/vbaProject.bin", b"Attribute VB_Name = \"AutoOpen\"\nShell \"calc.exe\"")
    return path


def xlsx(path: Path) -> Path:
    import openpyxl
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Prices"
    sheet.append(["Item", "Unit price", "Quantity", "Total"])
    sheet.append(["Widget", 2.5, 4, "=B2*C2"])
    sheet.append(["Gadget", 10, 1, "=B3*C3"])
    second = book.create_sheet("Notes")
    second["A1"] = "Discounts must not exceed 15 percent."
    book.save(path)
    return path


def zip_bomb(path: Path) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", DOCX_TYPES)
        archive.writestr("word/document.xml", b"<a>" + b" " * 60_000_000 + b"</a>")
    return path
