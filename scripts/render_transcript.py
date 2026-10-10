"""Render captured terminal output to a PNG (a faithful image of the text, not a screen capture).

    python scripts/render_transcript.py transcript.txt transcript.png "Windows PowerShell 5.1"
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CANDIDATES = ["C:/Windows/Fonts/consola.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
              "/System/Library/Fonts/Menlo.ttc"]


def main(source: str, target: str, title: str = "") -> None:
    text = Path(source).read_text(encoding="utf-8", errors="replace").rstrip()
    lines = ([f"[{title}] captured output, rendered"] if title else []) + text.splitlines()
    font = next((ImageFont.truetype(path, 15) for path in CANDIDATES if Path(path).exists()), ImageFont.load_default())
    width = max(font.getlength(line) for line in lines) + 32
    image = Image.new("RGB", (int(width), 20 * len(lines) + 24), "#0c0c0c")
    draw = ImageDraw.Draw(image)
    for number, line in enumerate(lines):
        color = "#9cdcfe" if number == 0 and title else ("#f14c4c" if "] x " in line else
                                                         "#23d18b" if "] + " in line else "#cccccc")
        draw.text((16, 12 + 20 * number), line, fill=color, font=font)
    image.save(target)


if __name__ == "__main__":
    main(*sys.argv[1:])
