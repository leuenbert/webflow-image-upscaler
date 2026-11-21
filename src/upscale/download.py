"""Stage 2: download every unique source URL into cache/download/."""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from PIL import Image
from tqdm import tqdm

from .common import Manifest, atomic_write_bytes, file_matches, oriented_size, sha256_file
from .config import Config
from .parse import Parsed, Source

log = logging.getLogger("upscale")

_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif",
        "image/tiff": "tif", "image/avif": "avif"}


def _ext_for(url: str, content_type: str) -> str:
    ct = content_type.split(";")[0].strip().lower()
    if ct in _EXT:
        return _EXT[ct]
    tail = url.rsplit("/", 1)[-1].rsplit(".", 1)
    return tail[1].lower()[:5] if len(tail) == 2 else "img"


def describe(path: Path) -> dict:
    with Image.open(path) as im:
        fmt, mode = im.format, im.mode
        has_icc = bool(im.info.get("icc_profile"))
    w, h = oriented_size(path)
    return {"path": path.name, "sha256": sha256_file(path), "width": w, "height": h,
            "format": fmt, "mode": mode, "bytes": path.stat().st_size, "has_icc": has_icc}


def _fetch(src: Source, cfg: Config) -> tuple[Source, dict]:
    d = cfg.download
    retries, timeout, backoff = int(d["retries"]), float(d["timeout_s"]), float(d["backoff_base_s"])
    headers = {"User-Agent": "webflow-upscale/0.1 (python-requests)"}
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            # URL used exactly as exported; requests does not re-encode existing %XX escapes
            r = requests.get(src.url, headers=headers, timeout=timeout)
            ct = r.headers.get("Content-Type", "")
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            if not ct.lower().startswith("image/"):
                raise RuntimeError(f"unexpected Content-Type {ct!r}")
            path = cfg.download_dir / f"{src.key}.{_ext_for(src.url, ct)}"
            atomic_write_bytes(path, r.content)
            info = describe(path)  # also proves the file decodes
            info["content_type"] = ct
            return src, info
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
    raise RuntimeError(f"download failed after {retries + 1} attempts: {last_err}")


def run_download(parsed: Parsed, cfg: Config, manifest: Manifest, dry_run: bool = False) -> list[str]:
    cfg.download_dir.mkdir(parents=True, exist_ok=True)
    todo: list[Source] = []
    for src in parsed.sources.values():
        rec = manifest.get(src.key, "download")
        if rec and file_matches(cfg.download_dir / rec["path"], rec["sha256"]):
            continue
        todo.append(src)

    log.info("download: %d unique URLs, %d cached, %d to fetch",
             len(parsed.sources), len(parsed.sources) - len(todo), len(todo))
    if dry_run:
        for s in todo:
            print(f"[dry-run] download {s.url}")
        return []

    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=int(cfg.download["workers"])) as pool:
        futs = {pool.submit(_fetch, s, cfg): s for s in todo}
        for fut in tqdm(as_completed(futs), total=len(futs), desc="download", unit="img"):
            src = futs[fut]
            try:
                _, info = fut.result()
                info["url"] = src.url
                manifest.set(src.key, "download", info)
                manifest.entry(src.key)["url"] = src.url
                manifest.save()
            except Exception as e:
                log.error("download %s: %s", src.url, e)
                failures.append(src.url)
    return failures
