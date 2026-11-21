"""index.csv / index.json (the upload index), plus `status` and `mark`."""
from __future__ import annotations

import csv
import io
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from fnmatch import fnmatch

from .common import Manifest, atomic_write_text, file_matches
from .config import Config
from .encode import quality_flag
from .parse import Parsed

log = logging.getLogger("upscale")

COLUMNS = [
    "slot_id", "item_name", "item_slug", "item_id", "collection_id", "is_draft",
    "field_label", "field_type", "position", "count_in_field", "source_url", "source_w", "source_h",
    "local_path", "out_w", "out_h", "out_bytes", "sha256", "method", "quality_flag", "verify",
    "upload_status", "uploaded_at", "notes",
]
# not_ready = the output file has not been built yet (e.g. after a --limit test run)
STATUSES = ("not_ready", "pending", "uploaded", "verified", "failed", "skipped")
_KEEP = ("uploaded", "verified", "failed", "skipped")


def read_index(cfg: Config) -> list[dict]:
    if not cfg.index_csv.is_file():
        return []
    with open(cfg.index_csv, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_csv(cfg: Config, rows: list[dict]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    atomic_write_text(cfg.index_csv, buf.getvalue())


def _write_json(cfg: Config, rows: list[dict]) -> None:
    items: dict[str, dict] = {}
    for r in rows:
        it = items.setdefault(r["item_slug"], {
            "item_name": r["item_name"], "item_slug": r["item_slug"], "item_id": r["item_id"],
            "collection_id": r["collection_id"], "is_draft": r["is_draft"] == "true", "fields": {}})
        fd = it["fields"].setdefault(r["field_label"], {
            "field_label": r["field_label"], "field_type": r["field_type"],
            "count_in_field": int(r["count_in_field"]), "files": []})
        f = {k: r[k] for k in COLUMNS if k not in it and k not in fd}
        for k in ("position", "source_w", "source_h", "out_w", "out_h", "out_bytes"):
            f[k] = int(f[k]) if f.get(k) else None
        fd["files"].append(f)
    out_items = []
    for it in items.values():
        it["fields"] = list(it["fields"].values())
        statuses = [f["upload_status"] for fd in it["fields"] for f in fd["files"]]
        it["item_status"] = ("done" if all(s in ("uploaded", "verified", "skipped") for s in statuses)
                             else "pending")
        out_items.append(it)
    doc = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "root": str(cfg.root), "slot_count": len(rows), "items": out_items}
    atomic_write_text(cfg.index_json, json.dumps(doc, indent=2, ensure_ascii=False))


def write_index(parsed_all: Parsed, cfg: Config, manifest: Manifest) -> list[dict]:
    """Write the index for *all* slots in the CSV, preserving upload progress from a previous index."""
    old = {r["slot_id"]: r for r in read_index(cfg)}
    vpath = cfg.cache_dir / "verify.json"
    vres = json.loads(vpath.read_text("utf-8")) if vpath.is_file() else {}
    rows = []
    for s in parsed_all.slots:
        dl = manifest.get(s.url_key, "download") or {}
        up = manifest.get(s.url_key, "upscale") or {}
        enc = manifest.get(s.url_key, "encode") or {}
        local = cfg.output_dir / s.out_rel
        built = bool(enc) and file_matches(local, enc.get("sha256"))
        vr = vres.get(s.slot_id, {})
        prev = old.get(s.slot_id, {})
        status = prev.get("upload_status", "")
        notes = prev.get("notes", "")
        if status not in _KEEP:
            status = "pending" if built else "not_ready"
        elif prev.get("sha256") and enc.get("sha256") and prev["sha256"] != enc["sha256"]:
            if "file changed since upload" not in notes:
                notes = (notes + "; " if notes else "") + "file changed since upload"
        rows.append({
            "slot_id": s.slot_id, "item_name": s.item.name, "item_slug": s.item.slug,
            "item_id": s.item.item_id, "collection_id": s.item.collection_id,
            "is_draft": "true" if s.item.is_draft else "false",
            "field_label": s.field.label, "field_type": "multi" if s.field.multi else "single",
            "position": s.position, "count_in_field": s.count_in_field, "source_url": s.source_url,
            "source_w": dl.get("width", ""), "source_h": dl.get("height", ""),
            "local_path": str(local.resolve()) if built else "",
            "out_w": enc.get("width", "") if built else "", "out_h": enc.get("height", "") if built else "",
            "out_bytes": enc.get("bytes", "") if built else "", "sha256": enc.get("sha256", "") if built else "",
            "method": up.get("method", ""), "quality_flag": quality_flag(enc) if built else "",
            "verify": ("ok" if vr.get("ok") else "; ".join(vr.get("problems", []))) if built else "",
            "upload_status": status, "uploaded_at": prev.get("uploaded_at", ""), "notes": notes,
        })
    _write_csv(cfg, rows)
    _write_json(cfg, rows)
    log.info("index: %d rows, %d ready", len(rows), sum(r["upload_status"] != "not_ready" for r in rows))
    return rows


def status(cfg: Config) -> None:
    rows = read_index(cfg)
    if not rows:
        print("No index.csv yet. Run `python -m upscale run` first.")
        return
    c = Counter(r["upload_status"] for r in rows)
    items = {}
    for r in rows:
        items.setdefault(r["item_slug"], []).append(r["upload_status"])
    done_items = sum(all(s in ("uploaded", "verified", "skipped") for s in v) for v in items.values())
    print(f"Slots: {len(rows)}   unique files: {len({r['sha256'] for r in rows if r['sha256']})}"
          f"   items: {done_items}/{len(items)} done")
    for s in STATUSES:
        if c.get(s):
            print(f"  {s:<10} {c[s]:>4}")
    flags = Counter(f for r in rows for f in r["quality_flag"].split(";") if f)
    if flags:
        print("Quality flags: " + ", ".join(f"{k}={v}" for k, v in flags.items()))
    vbad = [r["slot_id"] for r in rows if r["verify"] not in ("", "ok")]
    if vbad:
        print(f"Verify problems: {len(vbad)} (e.g. {', '.join(vbad[:3])})")
    failed = [r for r in rows if r["upload_status"] == "failed"]
    for r in failed:
        print(f"  FAILED {r['slot_id']}: {r['notes']}")
    nxt = next((r for r in rows if r["upload_status"] == "pending"), None)
    if nxt:
        print(f"Next pending: {nxt['slot_id']}  ({nxt['item_name']})")


def mark(cfg: Config, slot_id: str, new_status: str, note: str | None) -> None:
    if new_status not in STATUSES:
        raise SystemExit(f"status must be one of: {', '.join(STATUSES)}")
    rows = read_index(cfg)
    # exact id, or a glob such as "biglen-schulhaus-2cb59:gallery:*"
    hit = [r for r in rows if r["slot_id"] == slot_id] or [r for r in rows if fnmatch(r["slot_id"], slot_id)]
    if not hit:
        raise SystemExit(f"unknown slot_id {slot_id!r}")
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    for r in hit:
        r["upload_status"] = new_status
        if new_status in ("uploaded", "verified"):
            r["uploaded_at"] = now
        if note:
            r["notes"] = (r["notes"] + "; " if r["notes"] else "") + note
        print(f"{r['slot_id']} -> {new_status}")
    _write_csv(cfg, rows)
    _write_json(cfg, rows)
