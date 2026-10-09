"""Local OCR with Tesseract: text, word boxes and confidence from screenshots and scans.

Tesseract runs as a separate local program (no network). It is used only for images and pages
without reliable embedded text; PDFs and Office documents with real text are read directly.
Cloud OCR is never used.
"""
import os
import shutil
import subprocess
import time
from pathlib import Path

WINDOWS_PATHS = (r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe")


class OCRUnavailable(RuntimeError):
    pass


def find_tesseract(configured: str | None = None) -> str | None:
    if configured:
        return configured if Path(configured).is_file() else shutil.which(configured)
    found = shutil.which("tesseract")
    if found:
        return found
    if os.name == "nt":
        for candidate in [*WINDOWS_PATHS, str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR"
                                              / "tesseract.exe")]:
            if Path(candidate).is_file():
                return candidate
    return None


class TesseractOCR:
    name = "tesseract"

    def __init__(self, command: str | None = None, languages: str = "eng", timeout: int = 120,
                 min_confidence: float = 30.0):
        self.command = find_tesseract(command)
        self.languages, self.timeout, self.min_confidence = languages, timeout, min_confidence

    @property
    def available(self) -> bool:
        return bool(self.command)

    def version(self) -> str | None:
        if not self.command:
            return None
        try:
            completed = subprocess.run([self.command, "--version"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return None
        first = (completed.stdout or completed.stderr).strip().splitlines()
        return first[0] if first else None

    def recognize(self, image_path: Path, loc: str = "image") -> dict:
        """Words with boxes (pixels of the analyzed image) and confidence, grouped into lines."""
        if not self.command:
            raise OCRUnavailable("Tesseract is not installed")
        from PIL import Image, ImageOps
        started = time.monotonic()
        prepared = Path(image_path).with_suffix(".ocr.png")
        with Image.open(image_path) as image:
            gray = ImageOps.grayscale(image)
            scale = 2 if max(gray.size) < 1400 else 1  # small UI text recognizes better enlarged
            if scale > 1:
                gray = gray.resize((gray.size[0] * scale, gray.size[1] * scale), Image.LANCZOS)
            if gray.resize((1, 1)).getpixel((0, 0)) < 110:  # dark theme: Tesseract prefers dark text on light
                gray = ImageOps.invert(gray)
            gray.save(prepared)
        try:
            completed = subprocess.run([self.command, str(prepared), "stdout", "-l", self.languages, "--psm", "3",
                                        "tsv"], capture_output=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as error:
            raise OCRUnavailable(f"OCR timed out after {self.timeout} s") from error
        finally:
            prepared.unlink(missing_ok=True)
        if completed.returncode != 0:
            raise OCRUnavailable(completed.stderr.decode(errors="replace").strip()[-300:] or "Tesseract failed")
        return parse_tsv(completed.stdout.decode("utf-8", errors="replace"), scale, loc, self.min_confidence,
                         round(time.monotonic() - started, 2))


def parse_tsv(tsv: str, scale: int, loc: str, min_confidence: float, seconds: float) -> dict:
    lines, words = {}, []
    rows = tsv.splitlines()
    for row in rows[1:]:
        fields = row.split("\t")
        if len(fields) < 12 or fields[0] != "5":
            continue
        text = fields[11].strip()
        try:
            confidence = float(fields[10])
        except ValueError:
            continue
        if not text or confidence < 0:
            continue
        left, top, width, height = (int(value) // scale for value in fields[6:10])
        word = {"text": text, "confidence": round(confidence, 1), "box": [left, top, width, height]}
        words.append(word)
        lines.setdefault(tuple(fields[1:5]), []).append(word)
    grouped = []
    for members in lines.values():
        left = min(word["box"][0] for word in members)
        top = min(word["box"][1] for word in members)
        right = max(word["box"][0] + word["box"][2] for word in members)
        bottom = max(word["box"][1] + word["box"][3] for word in members)
        grouped.append({"text": " ".join(word["text"] for word in members),
                        "confidence": round(sum(word["confidence"] for word in members) / len(members), 1),
                        "box": [left, top, right - left, bottom - top]})
    grouped.sort(key=lambda line: (line["box"][1], line["box"][0]))
    reliable = [line for line in grouped if line["confidence"] >= min_confidence]
    mean = round(sum(word["confidence"] for word in words) / len(words), 1) if words else None
    return {"loc": loc, "engine": "tesseract", "text": "\n".join(line["text"] for line in reliable),
            "confidence": mean, "lines": grouped[:300], "low_confidence_lines": len(grouped) - len(reliable),
            "seconds": seconds}
