"""Lossless, ordered OCR pages. Never extract untrusted ZIP paths."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import zipfile

PROTOCOL = "jbb-ocr-pages-1"
MAX_BYTES = 500 * 1024 * 1024
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_RAW_BYTES = 2 * 1024 * 1024 * 1024


class TooLarge(ValueError):
    pass


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pack(paths, target, max_pages):
    if not 0 < len(paths) <= max_pages:
        raise ValueError("Invalid OCR page count")
    manifest = []
    total = 0
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for index, path in enumerate(paths):
            size = Path(path).stat().st_size
            total += size
            if not 0 < size <= MAX_PAGE_BYTES or total > MAX_RAW_BYTES:
                raise TooLarge("Oversized OCR pages")
            name = f"{index:04d}.pgm"
            archive.write(path, name)
            manifest.append({"name": name, "size": size, "sha256": file_digest(path)})
        archive.writestr("manifest.json", json.dumps({"protocol": PROTOCOL, "pages": manifest}, separators=(",", ":")))


def unpack(source, directory, count, max_pages):
    if not 0 < count <= max_pages:
        raise ValueError("Invalid OCR page count")
    paths = []
    with zipfile.ZipFile(source) as archive:
        names = [f"{index:04d}.pgm" for index in range(count)]
        entries = archive.infolist()
        if len(entries) != count + 1 or {i.filename for i in entries} != {*names, "manifest.json"}:
            raise ValueError("Invalid OCR archive entries")
        info = archive.getinfo("manifest.json")
        if info.file_size > 1024 * 1024:
            raise ValueError("Oversized OCR manifest")
        manifest = json.loads(archive.read(info))
        if manifest.get("protocol") != PROTOCOL or len(manifest.get("pages", [])) != count:
            raise ValueError("Invalid OCR manifest")
        total = 0
        for name, expected in zip(names, manifest["pages"]):
            info = archive.getinfo(name)
            total += info.file_size
            if (expected.get("name") != name or expected.get("size") != info.file_size
                    or not 0 < info.file_size <= MAX_PAGE_BYTES or total > MAX_RAW_BYTES):
                raise ValueError("Invalid OCR page size or order")
            # Bounded read also rejects truncated ZIP streams / CRC failures.
            data = archive.read(info)
            if not data.startswith(b"P5\n") or hashlib.sha256(data).hexdigest() != expected.get("sha256"):
                raise ValueError("Invalid OCR page pixels")
            path = Path(directory) / name
            path.write_bytes(data)
            paths.append(path)
    return paths
