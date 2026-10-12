"""Add the matching native Windows installer to an already-built backend release.

This runs only on the owner-approved Windows release runner, after compiling the desktop
from exactly the approved commit and verifying its installed self-test. The release manifest
declares the matching version; SHA256SUMS covers *all* published files, including the EXE.
"""
import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path


def finalize(out: Path, installer: Path, portable: Path) -> dict:
    out = Path(out).resolve(strict=True)
    manifest = out / "release.json"
    info = json.loads(manifest.read_text(encoding="utf-8"))
    version = info.get("version")
    if not isinstance(version, str) or not re.fullmatch(
            r"\d+\.\d+\.\d+(?:(?:a|b|rc|\.dev)\d+)?(?:\+[a-z0-9]+(?:[._-][a-z0-9]+)*)?", version):
        raise ValueError("invalid release manifest version")
    if info.get("tag") != "v" + version:
        raise ValueError("release manifest tag/version mismatch")
    if info.get("desktop") is not None:
        raise ValueError("desktop release already finalized")
    assets = {"installer": (Path(installer), "CodingBrain-Setup.exe"),
              "portable": (Path(portable), "CodingBrainDesktop-Windows.zip")}
    for label, (source, name) in assets.items():
        if source.is_symlink() or not source.is_file() or source.stat().st_size < 1024:
            raise ValueError(f"invalid desktop {label} asset: {source}")
        target = out / name
        if target.exists():
            raise ValueError(f"desktop asset already exists: {name}")
        shutil.copyfile(source, target)
    info["desktop"] = {"version": version,
                       "installer": assets["installer"][1],
                       "portable": assets["portable"][1]}
    manifest.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    files = sorted(p for p in out.iterdir() if p.is_file() and p.name != "SHA256SUMS")
    (out / "SHA256SUMS").write_text(
        "\n".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p in files) + "\n",
        encoding="utf-8")
    return info


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--installer", required=True)
    parser.add_argument("--portable", required=True)
    args = parser.parse_args()
    print(json.dumps(finalize(Path(args.out), Path(args.installer), Path(args.portable)), indent=2))


if __name__ == "__main__":
    main()
