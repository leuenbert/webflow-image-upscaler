"""Stage 3: produce a high-resolution master per unique source.

Order of preference: a user-supplied original in originals_override/,
a plain downscale when the source is already wide enough, else Real-ESRGAN.
"""
from __future__ import annotations

import csv
import io
import logging
import subprocess
from pathlib import Path

from PIL import Image, ImageStat
from tqdm import tqdm

from .common import Manifest, atomic_write_text, file_matches, open_normalized, oriented_size, sha256_file
from .config import Config
from .parse import Parsed, Source

log = logging.getLogger("upscale")

OVERRIDE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
MAP_COLUMNS = ["url_key", "decoded_stem", "item_slugs", "slot_ids", "source_width", "source_url",
               "auto_match", "manual_override"]


# --- originals_override ------------------------------------------------------

def _scan_overrides(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in OVERRIDE_EXTS
                  and not p.name.startswith("."))


def _auto_match(src: Source, files: list[Path], root: Path, stem_counts: dict[str, int]) -> Path | None:
    stem = src.stem.casefold()
    slugs = {s.item.slug for s in src.slots}
    cands = []
    for f in files:
        if f.stem.casefold() != stem:
            continue
        rel = f.relative_to(root)
        if len(rel.parts) > 1:              # originals_override/<slug>/<stem>.jpg
            if rel.parts[0] in slugs:
                cands.append(f)
        elif stem_counts.get(stem, 0) == 1:  # top-level only when the stem is unambiguous
            cands.append(f)
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        log.warning("override for %s is ambiguous (%s); set manual_override in originals_map.csv",
                    src.stem, ", ".join(str(c.relative_to(root)) for c in cands))
    return None


def resolve_overrides(parsed: Parsed, all_sources: dict[str, Source], cfg: Config,
                      manifest: Manifest, write_map: bool = True) -> dict[str, Path]:
    """Return url_key -> override file, and (re)write originals_map.csv.

    `manual_override` values typed by the user are preserved across runs; use a path relative
    to originals_override/ (or absolute). The value "none" disables a wrong auto match.
    """
    root = cfg.originals_dir
    files = _scan_overrides(root)
    manual: dict[str, str] = {}
    if cfg.originals_map.is_file():
        with open(cfg.originals_map, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("manual_override", "").strip():
                    manual[row["url_key"]] = row["manual_override"].strip()

    stem_counts: dict[str, int] = {}
    for s in all_sources.values():
        stem_counts[s.stem.casefold()] = stem_counts.get(s.stem.casefold(), 0) + 1

    result: dict[str, Path] = {}
    rows = []
    for src in all_sources.values():
        auto = _auto_match(src, files, root, stem_counts)
        chosen: Path | None = auto
        m = manual.get(src.key, "")
        if m.lower() == "none":
            chosen = None
        elif m:
            p = Path(m).expanduser()
            p = p if p.is_absolute() else root / p
            if p.is_file():
                chosen = p
            else:
                log.warning("manual_override %s for %s does not exist; ignoring", m, src.stem)
        if chosen and src.key in parsed.sources:
            result[src.key] = chosen
        dl = manifest.get(src.key, "download") or {}
        rows.append({
            "url_key": src.key,
            "decoded_stem": src.stem,
            "item_slugs": " ".join(dict.fromkeys(s.item.slug for s in src.slots)),
            "slot_ids": " ".join(s.slot_id for s in src.slots),
            "source_width": dl.get("width", ""),
            "source_url": src.url,
            "auto_match": str(auto.relative_to(root)) if auto else "",
            "manual_override": m,
        })
    if write_map:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=MAP_COLUMNS)
        w.writeheader()
        w.writerows(rows)
        atomic_write_text(cfg.originals_map, buf.getvalue())
    return result


# --- Real-ESRGAN ---------------------------------------------------------------

def _is_blank(im: Image.Image, min_std: float) -> bool:
    small = im.convert("L").resize((256, max(1, round(256 * im.height / im.width))))
    return ImageStat.Stat(small).stddev[0] < min_std


def check_binary(cfg: Config) -> Path:
    b = cfg.esrgan["binary"]
    if not b.is_file():
        raise SystemExit(f"Real-ESRGAN binary not found at {b}. Run scripts/setup.sh first.")
    return b


def run_realesrgan(inp: Path, out: Path, cfg: Config) -> int:
    """Run the ncnn-vulkan binary, falling back to smaller tiles on failure. Returns the tile used."""
    e = cfg.esrgan
    binary = check_binary(cfg)
    with Image.open(inp) as im:
        expect = (im.width * int(e["scale"]), im.height * int(e["scale"]))
    errors = []
    for tile in e["tile_sizes"]:
        out.unlink(missing_ok=True)
        cmd = [str(binary), "-i", str(inp), "-o", str(out), "-n", e["model"], "-s", str(e["scale"]),
               "-t", str(tile), "-m", str(e["models_dir"]), "-f", "png"]
        if str(e.get("gpu_id", "auto")).lower() != "auto":
            cmd += ["-g", str(e["gpu_id"])]
        try:
            proc = subprocess.run(cmd, cwd=binary.parent, capture_output=True, text=True,
                                  timeout=int(e.get("timeout_s", 900)))
        except subprocess.TimeoutExpired:
            errors.append(f"tile {tile}: timeout")
            continue
        msg = (proc.stderr or "") + (proc.stdout or "")
        ok = proc.returncode == 0 and out.is_file()
        if ok:
            try:
                with Image.open(out) as res:
                    res.load()
                    if res.size != expect:
                        ok, why = False, f"unexpected size {res.size}, expected {expect}"
                    elif _is_blank(res, 0.5):
                        ok, why = False, "blank output (likely GPU out of memory)"
            except Exception as ex:
                ok, why = False, f"unreadable output: {ex}"
        else:
            why = f"exit {proc.returncode}: {msg.strip()[-300:]}"
        if ok:
            return tile
        errors.append(f"tile {tile}: {why}")
        log.warning("Real-ESRGAN %s with tile %s failed: %s", inp.name, tile, why)
    raise RuntimeError("Real-ESRGAN failed: " + " | ".join(errors))


# --- stage ---------------------------------------------------------------------

def plan_source(src: Source, cfg: Config, manifest: Manifest, overrides: dict[str, Path]) -> dict:
    """Decide which input and method to use for one source."""
    dl = manifest.get(src.key, "download")
    if not dl:
        raise RuntimeError("not downloaded")
    override = overrides.get(src.key)
    inp = override if override else cfg.download_dir / dl["path"]
    w, h = oriented_size(inp)
    ai = w < cfg.target_width
    if override:
        method = "original_override"
    else:
        method = "realesrgan_x4plus" if ai else "downscale_only"
    return {"input": inp, "input_w": w, "input_h": h, "ai": ai, "method": method}


def _params(cfg: Config) -> dict:
    e = cfg.esrgan
    return {"model": e["model"], "scale": int(e["scale"])}


def _rel(p: Path, cfg: Config) -> str:
    try:
        return str(p.relative_to(cfg.root))
    except ValueError:
        return str(p)


def master_path(rec: dict, cfg: Config) -> Path:
    p = Path(rec["raw_path"] if rec["ai"] else rec["input_path"])
    return p if p.is_absolute() else cfg.root / p


def run_upscale(parsed: Parsed, all_sources: dict[str, Source], cfg: Config, manifest: Manifest,
                dry_run: bool = False) -> list[str]:
    overrides = resolve_overrides(parsed, all_sources, cfg, manifest, write_map=not dry_run)
    cfg.raw_dir.mkdir(parents=True, exist_ok=True)
    cfg.prepared_dir.mkdir(parents=True, exist_ok=True)

    todo: list[tuple[Source, dict, str]] = []
    failures: list[str] = []
    for src in parsed.sources.values():
        try:
            plan = plan_source(src, cfg, manifest, overrides)
        except Exception as e:
            log.error("upscale %s: %s", src.url, e)
            failures.append(src.url)
            continue
        input_sha = sha256_file(plan["input"])
        rec = manifest.get(src.key, "upscale")
        if (rec and rec.get("input_sha256") == input_sha and rec.get("ai") == plan["ai"]
                and (not plan["ai"] or (rec.get("params") == _params(cfg)
                                         and file_matches(cfg.root / rec["raw_path"], rec.get("raw_sha256"))))):
            continue
        todo.append((src, plan, input_sha))

    n_ai = sum(1 for _, p, _ in todo if p["ai"])
    log.info("upscale: %d sources, %d up to date, %d to do (%d with Real-ESRGAN, %d overrides)",
             len(parsed.sources), len(parsed.sources) - len(todo), len(todo), n_ai, len(overrides))
    if dry_run:
        for src, plan, _ in todo:
            print(f"[dry-run] {plan['method']:<18} ai={plan['ai']!s:<5} {plan['input_w']}px  {src.stem}")
        return failures

    if n_ai:
        check_binary(cfg)
    for src, plan, input_sha in tqdm(todo, desc="upscale", unit="img"):
        rec = {"method": plan["method"], "ai": plan["ai"], "input_path": _rel(plan["input"], cfg),
               "input_sha256": input_sha, "input_w": plan["input_w"], "input_h": plan["input_h"],
               "params": _params(cfg)}
        try:
            if plan["ai"]:
                prepared = cfg.prepared_dir / f"{src.key}.png"
                raw = cfg.raw_dir / f"{src.key}.png"
                open_normalized(plan["input"]).save(prepared, format="PNG", compress_level=1)
                tile = run_realesrgan(prepared, raw, cfg)
                prepared.unlink(missing_ok=True)
                with Image.open(raw) as im:
                    rec.update(raw_w=im.width, raw_h=im.height)
                rec.update(raw_path=_rel(raw, cfg), raw_sha256=sha256_file(raw), tile=tile)
            manifest.set(src.key, "upscale", rec)
            manifest.drop(src.key, "encode")
            manifest.save()
        except Exception as e:
            log.error("upscale %s: %s", src.url, e)
            failures.append(src.url)
    return failures
