"""Command line: python -m upscale <command>."""
from __future__ import annotations

import argparse
import logging
import sys

from .common import Manifest
from .config import Config, load_config
from .parse import Parsed, parse_csv, select

log = logging.getLogger("upscale")


def _setup_logging(cfg: Config, verbose: bool) -> None:
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG)
    fh = logging.FileHandler(cfg.logs_dir / "pipeline.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    fh.setLevel(logging.DEBUG)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(fh)
    log.addHandler(sh)


def _load(cfg: Config, args) -> tuple[Parsed, Parsed]:
    full = parse_csv(cfg.input_csv)
    return full, select(full, args.only_slug, args.limit)


def cmd_parse(cfg, args, full, sel, manifest):
    n_draft = sum(i.is_draft for i in sel.items)
    print(f"{len(sel.items)} items ({n_draft} drafts), {len(sel.slots)} slots, {len(sel.sources)} unique URLs")
    per_field: dict[str, int] = {}
    for s in sel.slots:
        per_field[s.field.label] = per_field.get(s.field.label, 0) + 1
    for k, v in per_field.items():
        print(f"  {k:<18} {v}")
    shared = [s for s in sel.sources.values() if len(s.slots) > 1]
    for s in shared:
        print(f"  reused URL ({len(s.slots)} slots): {', '.join(x.slot_id for x in s.slots)}")
    return []


def cmd_download(cfg, args, full, sel, manifest):
    from .download import run_download
    return run_download(sel, cfg, manifest, args.dry_run)


def cmd_upscale(cfg, args, full, sel, manifest):
    from .upscale import run_upscale
    return run_upscale(sel, full.sources, cfg, manifest, args.dry_run)


def cmd_encode(cfg, args, full, sel, manifest):
    from .encode import run_encode
    return run_encode(sel, cfg, manifest, args.dry_run)


def cmd_verify(cfg, args, full, sel, manifest):
    from .verify import run_verify
    if args.dry_run:
        print("[dry-run] would verify outputs and write review.html")
        return []
    res = run_verify(sel, cfg, manifest)
    return [k for k, v in res.items() if not v["ok"] and k in {s.slot_id for s in sel.slots}]


def cmd_index(cfg, args, full, sel, manifest):
    from .index import write_index
    from .runbook import write_runbook
    if args.dry_run:
        print("[dry-run] would write index.csv, index.json and UPLOAD_RUNBOOK.md")
        return []
    rows = write_index(full, cfg, manifest)
    write_runbook(cfg, rows)
    return []


STAGES = [("parse", cmd_parse), ("download", cmd_download), ("upscale", cmd_upscale),
          ("encode", cmd_encode), ("verify", cmd_verify), ("index", cmd_index)]


def cmd_run(cfg, args, full, sel, manifest):
    problems: dict[str, list] = {}
    for name, fn in STAGES:
        log.info("=== %s ===", name)
        bad = fn(cfg, args, full, sel, manifest)
        if bad:
            problems[name] = bad
    if args.dry_run:
        return []
    print()
    if problems:
        for k, v in problems.items():
            print(f"{k}: {len(v)} problem(s) — see logs/pipeline.log")
    print(f"Review the results before uploading:  open \"{cfg.review_html}\"")
    print("Then hand UPLOAD_RUNBOOK.md to Claude in Chrome.")
    return [x for v in problems.values() for x in v]


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--only-slug", action="append", metavar="SLUG",
                        help="process only this item (repeatable)")
    common.add_argument("--limit", type=int, metavar="N", help="process only the first N items")
    common.add_argument("--dry-run", action="store_true", help="show what would be done, change nothing")
    common.add_argument("-v", "--verbose", action="store_true")

    p = argparse.ArgumentParser(prog="python -m upscale",
                                description="Upscale Webflow CMS images and build the upload index.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", parents=[common], help="all stages: parse → download → upscale → encode → verify → index")
    for name, _ in STAGES:
        sub.add_parser(name, parents=[common], help=f"run only the {name} stage")
    sub.add_parser("status", help="upload progress summary from index.csv")
    m = sub.add_parser("mark", help="set upload_status of a slot (glob patterns allowed)")
    m.add_argument("slot_id")
    m.add_argument("status")
    m.add_argument("--note")

    args = p.parse_args(argv)
    cfg = load_config()

    if args.cmd == "status":
        from .index import status
        status(cfg)
        return 0
    if args.cmd == "mark":
        from .index import mark
        mark(cfg, args.slot_id, args.status, args.note)
        return 0

    _setup_logging(cfg, args.verbose)
    full, sel = _load(cfg, args)
    manifest = Manifest(cfg.manifest_path)
    fn = dict(STAGES + [("run", cmd_run)])[args.cmd]
    bad = fn(cfg, args, full, sel, manifest)
    return 1 if bad else 0
