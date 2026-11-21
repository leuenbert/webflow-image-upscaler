"""Shared helpers: hashing, the resumability manifest, image normalisation."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageCms, ImageOps

log = logging.getLogger("upscale")

Image.MAX_IMAGE_PIXELS = 400_000_000  # 4x outputs of large sources are big but legitimate


def sha1_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def file_matches(path: Path, sha256: str | None) -> bool:
    """True if `path` exists and its content hash equals the recorded one."""
    return bool(sha256) and path.is_file() and sha256_file(path) == sha256


class Manifest:
    """Per-unique-URL state, keyed by sha1(url). Stored in cache/manifest.json."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict[str, Any]] = {}
        if path.is_file():
            self.data = json.loads(path.read_text("utf-8"))

    def entry(self, key: str) -> dict[str, Any]:
        return self.data.setdefault(key, {})

    def get(self, key: str, stage: str) -> dict[str, Any] | None:
        return self.data.get(key, {}).get(stage)

    def set(self, key: str, stage: str, value: dict[str, Any]) -> None:
        self.entry(key)[stage] = value

    def drop(self, key: str, stage: str) -> None:
        self.data.get(key, {}).pop(stage, None)

    def save(self) -> None:
        atomic_write_text(self.path, json.dumps(self.data, indent=1, sort_keys=True))


# --- images ------------------------------------------------------------------

_SRGB = ImageCms.createProfile("sRGB")
SRGB_ICC_BYTES = ImageCms.ImageCmsProfile(_SRGB).tobytes()


def open_normalized(path: Path) -> Image.Image:
    """Open an image as 8-bit RGB in sRGB with EXIF orientation applied.

    Handles CMYK, 16-bit, palette, alpha and embedded ICC profiles.
    """
    im = Image.open(path)
    im.load()
    im = ImageOps.exif_transpose(im)
    icc = im.info.get("icc_profile")

    # 16-bit / 32-bit greyscale -> 8-bit
    if im.mode in ("I;16", "I;16B", "I;16L", "I;16N"):
        im = im.convert("I")
    if im.mode == "I":
        im = im.point(lambda v: v * (1 / 256)).convert("L")
    elif im.mode == "F":
        im = im.convert("L")

    # transparency -> flatten on white
    if im.mode in ("P", "PA"):
        im = im.convert("RGBA")
    if im.mode in ("RGBA", "LA", "La", "RGBa"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.getchannel("A"))
        im = bg

    if icc and im.mode in ("RGB", "CMYK", "L"):
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            im = ImageCms.profileToProfile(
                im, src, _SRGB, renderingIntent=ImageCms.Intent.PERCEPTUAL, outputMode="RGB"
            )
        except Exception as e:  # broken profile: fall back to a naive conversion
            log.warning("ICC conversion failed for %s (%s); converting naively", path.name, e)
    if im.mode != "RGB":
        im = im.convert("RGB")
    im.info.pop("icc_profile", None)
    return im


def oriented_size(path: Path) -> tuple[int, int]:
    """Display size (after EXIF orientation) without decoding the full image."""
    with Image.open(path) as im:
        w, h = im.size
        try:
            orientation = im.getexif().get(0x0112, 1)
        except Exception:
            orientation = 1
    return (h, w) if orientation in (5, 6, 7, 8) else (w, h)
