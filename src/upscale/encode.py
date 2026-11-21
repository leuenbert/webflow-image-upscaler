"""Stage 4: resize each master to the target width and write the final WebP.

Per unique source this produces
  cache/encoded/<key>.jpg    high-quality JPEG master (reference only, no size limit, never uploaded)
  cache/encoded/<key>.webp   the upload file, size-budgeted
and copies the WebP to output/<slug>/<slug>--<field>[-NN].webp for every slot that uses it.

The WebP is encoded with cwebp using the same settings as optimize_images.sh
(-q 82 -m 6 -sharp_yuv -metadata none -mt). It is encoded from the lossless in-memory image,
not from the JPEG, so there is no double compression. Quality is lowered only when the file
would exceed max_bytes.
"""
from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image
from tqdm import tqdm

from . import texture
from .common import Manifest, SRGB_ICC_BYTES, atomic_write_bytes, file_matches, open_normalized, sha256_file
from .config import Config
from .parse import OUTPUT_EXT, Parsed
from .upscale import master_path

log = logging.getLogger("upscale")

ENCODER_VERSION = 2


def _master_jpeg(im: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, progressive=True, optimize=True,
            subsampling=0, icc_profile=SRGB_ICC_BYTES)
    return buf.getvalue()


def check_cwebp(cfg: Config) -> Path:
    b = cfg.cwebp
    if b is None:
        raise SystemExit(f"cwebp not found ({cfg.webp['cwebp']!r}). Install it first: brew install webp")
    return b


def _cwebp(png: Path, quality: int, cfg: Config) -> bytes:
    w = cfg.webp
    with tempfile.TemporaryDirectory(dir=cfg.encoded_dir) as td:
        out = Path(td) / "out.webp"
        cmd = [str(check_cwebp(cfg)), "-quiet", "-q", str(quality), "-m", str(int(w["method"])),
               "-metadata", str(w["metadata"]), "-mt"]
        if w.get("sharp_yuv", True):
            cmd.append("-sharp_yuv")
        cmd += [str(png), "-o", str(out)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError(f"cwebp failed (exit {proc.returncode}): {proc.stderr.strip()[-300:]}")
        return out.read_bytes()


def encode_webp_budgeted(png: Path, cfg: Config, q_min: int, q_max: int,
                         max_bytes: int) -> tuple[bytes, int, bool]:
    """Highest quality in [q_min, q_max] that fits max_bytes. Returns (data, quality, over_budget).

    q_max is tried first; with the usual sizes it fits and only one encode is needed.
    """
    data = _cwebp(png, q_max, cfg)
    if len(data) <= max_bytes:
        return data, q_max, False
    best: tuple[bytes, int] | None = None
    lo, hi = q_min, q_max - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        d = _cwebp(png, mid, cfg)
        if len(d) <= max_bytes:
            best, lo = (d, mid), mid + 1
        else:
            hi = mid - 1
    if best:
        return best[0], best[1], False
    return _cwebp(png, q_min, cfg), q_min, True


def _params(cfg: Config) -> dict:
    w = cfg.webp
    return {"v": ENCODER_VERSION, "target_width": cfg.target_width, "max_bytes": int(cfg.out["max_bytes"]),
            "master_jpeg_quality": int(cfg.out["master_jpeg_quality"]),
            "webp": {"q_max": int(w["quality"]), "q_min": int(w["quality_min"]), "method": int(w["method"]),
                     "sharp_yuv": bool(w.get("sharp_yuv", True)), "metadata": str(w["metadata"])},
            "texture": texture.params_from(cfg.raw.get("texture"))}


def quality_flag(rec: dict) -> str:
    flags = []
    if rec.get("low_source"):
        flags.append("low_source")
    if rec.get("over_budget"):
        flags.append("size_over_budget")
    return ";".join(flags)


def run_encode(parsed: Parsed, cfg: Config, manifest: Manifest, dry_run: bool = False) -> list[str]:
    cfg.encoded_dir.mkdir(parents=True, exist_ok=True)
    params = _params(cfg)
    failures: list[str] = []

    todo = []
    for src in parsed.sources.values():
        up = manifest.get(src.key, "upscale")
        if not up:
            continue
        rec = manifest.get(src.key, "encode")
        master = master_path(up, cfg)
        if (rec and rec.get("params") == params
                and rec.get("master_sha256") == (up.get("raw_sha256") if up["ai"] else up["input_sha256"])
                and file_matches(cfg.root / rec["path"], rec["sha256"])):
            continue
        todo.append((src, up, master))

    log.info("encode: %d to encode", len(todo))
    if dry_run:
        for src, up, _ in todo:
            print(f"[dry-run] encode {src.stem} ({up['method']})")
    elif todo:
        check_cwebp(cfg)
        tw = cfg.target_width
        wp = params["webp"]
        for src, up, master in tqdm(todo, desc="encode", unit="img"):
            try:
                im = open_normalized(master)
                low_source = im.width < tw
                th = round(im.height * tw / im.width)
                if im.size != (tw, th):
                    im = im.resize((tw, th), Image.Resampling.LANCZOS)
                if up["ai"] and texture.is_active(params["texture"]):
                    source = open_normalized(cfg.root / up["input_path"])
                    im = texture.apply(im, source, params["texture"], seed=src.key)

                jpg = cfg.encoded_dir / f"{src.key}.jpg"
                atomic_write_bytes(jpg, _master_jpeg(im, params["master_jpeg_quality"]))

                png = cfg.encoded_dir / f"{src.key}.tmp.png"
                im.save(png, format="PNG", compress_level=1)
                try:
                    data, q, over = encode_webp_budgeted(png, cfg, wp["q_min"], wp["q_max"], params["max_bytes"])
                finally:
                    png.unlink(missing_ok=True)
                path = cfg.encoded_dir / f"{src.key}{OUTPUT_EXT}"
                atomic_write_bytes(path, data)

                rec = {"path": str(path.relative_to(cfg.root)), "sha256": sha256_file(path),
                       "master_jpeg": str(jpg.relative_to(cfg.root)), "master_jpeg_bytes": jpg.stat().st_size,
                       "master_sha256": up.get("raw_sha256") if up["ai"] else up["input_sha256"],
                       "params": params, "format": "webp", "quality": q, "bytes": len(data),
                       "width": tw, "height": th, "low_source": low_source, "over_budget": over}
                if over:
                    log.warning("%s is %d bytes even at WebP quality %d", src.stem, len(data), q)
                manifest.set(src.key, "encode", rec)
                manifest.save()
            except Exception as e:
                log.error("encode %s: %s", src.url, e)
                failures.append(src.url)

    # one file per slot (reused URLs get their own copy)
    copied = 0
    expected: dict[Path, set[str]] = {}
    for slot in parsed.slots:
        dst = cfg.output_dir / slot.out_rel
        expected.setdefault(dst.parent, set()).add(dst.name)
        rec = manifest.get(slot.url_key, "encode")
        if not rec or file_matches(dst, rec["sha256"]):
            continue
        if dry_run:
            print(f"[dry-run] write {dst.relative_to(cfg.root)}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cfg.root / rec["path"], dst)
        copied += 1
    log.info("encode: wrote %d slot files", copied)

    # remove leftovers in the processed item folders (e.g. .jpg files from the earlier JPEG output)
    if not dry_run:
        for folder, names in expected.items():
            if folder.is_dir():
                for f in folder.iterdir():
                    if f.is_file() and f.name not in names:
                        f.unlink()
                        log.info("removed stale output %s", f.relative_to(cfg.root))
    return failures
