"""Fetch the Monaco editor (the editor inside VS Code) into desktop_ui/static/vendor/monaco.

    python scripts/fetch_monaco.py            # once per checkout; the desktop build and CI run it

The desktop's Content-Security-Policy only allows its own files, so Monaco is bundled rather than
loaded from a CDN. One pinned release is downloaded from the npm registry and must match the
SHA-512 below before anything is extracted; only the minified editor (min/vs) is kept, without
source maps. Without these files the desktop falls back to its read-only text viewer.
"""
import base64
import hashlib
import io
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "0.52.2"
URL = f"https://registry.npmjs.org/monaco-editor/-/monaco-editor-{VERSION}.tgz"
SHA512 = "GEQWEZmfkOGLdd3XK8ryrfWz3AIP8YymVXiPHEdewrUq7mh0qrKrfHLNCXcbB6sTnMLnOZ3ztSiKcciFUkIJwQ=="
ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "desktop_ui" / "static" / "vendor" / "monaco"


def main() -> int:
    marker = TARGET / "VERSION"
    if marker.is_file() and marker.read_text().strip() == VERSION and (TARGET / "vs" / "loader.js").is_file() \
            and (TARGET / "LICENSE").is_file():
        print(f"Monaco {VERSION} already present")
        return 0
    print(f"Downloading Monaco {VERSION}")
    with urllib.request.urlopen(URL, timeout=120) as response:  # noqa: S310 - fixed https URL
        data = response.read()
    digest = base64.b64encode(hashlib.sha512(data).digest()).decode()
    if digest != SHA512:
        print(f"Checksum mismatch for {URL}; nothing was extracted", file=sys.stderr)
        return 1
    if TARGET.exists():
        shutil.rmtree(TARGET)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            name = member.name
            if name in ("package/LICENSE", "package/ThirdPartyNotices.txt") and member.isfile():
                (TARGET / Path(name).name).parent.mkdir(parents=True, exist_ok=True)
                (TARGET / Path(name).name).write_bytes(archive.extractfile(member).read())
                continue
            if not name.startswith("package/min/vs/") or name.endswith(".map") or not member.isfile():
                continue
            relative = Path(name[len("package/min/"):])
            if ".." in relative.parts:
                continue
            destination = TARGET / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.extractfile(member).read())
    marker.write_text(VERSION + "\n", encoding="utf-8")
    size = sum(path.stat().st_size for path in TARGET.rglob("*") if path.is_file())
    print(f"Monaco {VERSION} extracted to {TARGET} ({size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
