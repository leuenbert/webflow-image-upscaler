"""Stage 5: check every output file and write review.html."""
from __future__ import annotations

import html
import json
import logging
from pathlib import Path

from PIL import Image, ImageStat

from .common import Manifest, atomic_write_text
from .config import Config
from .encode import quality_flag
from .parse import Parsed, Slot

log = logging.getLogger("upscale")


def verify_file(path: Path, src_w: int, src_h: int, cfg: Config) -> dict:
    problems: list[str] = []
    res = {"exists": path.is_file(), "width": None, "height": None, "bytes": None}
    if not res["exists"]:
        return {**res, "ok": False, "problems": ["missing"]}
    res["bytes"] = path.stat().st_size
    try:
        with Image.open(path) as im:
            im.load()
            res["width"], res["height"] = im.size
            res["format"] = im.format
            small = im.convert("L").resize((256, max(1, round(256 * im.height / im.width))))
            st = ImageStat.Stat(small)
            hist = small.histogram()
    except Exception as e:
        return {**res, "ok": False, "problems": [f"does not open: {e}"]}

    if res["format"] != "WEBP":
        problems.append(f"format {res['format']} is not WebP")
    if res["width"] != cfg.target_width:
        problems.append(f"width {res['width']} != {cfg.target_width}")
    if res["bytes"] > int(cfg.out["max_bytes"]):
        problems.append(f"{res['bytes']} bytes > {cfg.out['max_bytes']}")
    if src_w and src_h:
        dev = abs((res["height"] / res["width"]) / (src_h / src_w) - 1)
        res["aspect_dev"] = round(dev, 5)
        if dev > float(cfg.verify["aspect_tolerance"]):
            problems.append(f"aspect ratio off by {dev:.2%}")
    if st.stddev[0] < float(cfg.verify["min_luma_stddev"]) or max(hist) > 0.98 * sum(hist):
        problems.append("looks blank (flat luminance histogram)")
    return {**res, "ok": not problems, "problems": problems}


def run_verify(parsed: Parsed, cfg: Config, manifest: Manifest) -> dict[str, dict]:
    results_path = cfg.cache_dir / "verify.json"
    results: dict[str, dict] = json.loads(results_path.read_text("utf-8")) if results_path.is_file() else {}
    bad = 0
    for slot in parsed.slots:
        dl = manifest.get(slot.url_key, "download") or {}
        r = verify_file(cfg.output_dir / slot.out_rel, dl.get("width", 0), dl.get("height", 0), cfg)
        results[slot.slot_id] = r
        if not r["ok"]:
            bad += 1
            log.warning("verify %s: %s", slot.slot_id, "; ".join(r["problems"]))
    atomic_write_text(results_path, json.dumps(results, indent=1, sort_keys=True))
    log.info("verify: %d slots checked, %d with problems", len(parsed.slots), bad)
    write_review(parsed, cfg, manifest, results)
    return results


# --- review.html -------------------------------------------------------------------

_CSS = """
:root{--bg:#f6f5f2;--fg:#1d1d1b;--muted:#6b6a66;--card:#fff;--line:#e2e0da;--warn:#b54708;--bad:#b42318;--ok:#067647}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe8;--muted:#9a9893;--card:#21211f;--line:#34332f;--warn:#f5a524;--bad:#f97066;--ok:#47cd89}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
header h1{font-size:16px;margin:0;font-weight:600}header .stats{color:var(--muted)}
button,label.tg{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:6px;padding:6px 10px;cursor:pointer}
main{padding:16px;max-width:1600px;margin:0 auto}
h2{font-size:15px;margin:28px 0 8px}h2 small{color:var(--muted);font-weight:400}
.pair{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.cell .vp{overflow:hidden;border-radius:4px;background:#888}
.cell img{display:block;width:100%;height:auto}
.meta{font-size:12px;color:var(--muted);margin:4px 0 0}
.title{font-weight:600;margin-bottom:6px;display:flex;gap:8px;flex-wrap:wrap;align-items:baseline}
.flag{font-size:11px;padding:1px 6px;border-radius:10px;border:1px solid currentColor}
.flag.warn{color:var(--warn)}.flag.bad{color:var(--bad)}.flag.ok{color:var(--ok)}
body.crop .cell .vp{height:520px;overflow:auto}
body.crop .cell img{width:var(--tw);max-width:none;image-rendering:auto}
body.onlyflagged .pair:not(.flagged){display:none}
@media (max-width:700px){.grid{grid-template-columns:1fr}}
"""

_JS = """
const b=document.body;
document.getElementById('crop').onclick=()=>{b.classList.toggle('crop');
  document.getElementById('crop').textContent=b.classList.contains('crop')?'Fit to width':'100% crop';
  if(b.classList.contains('crop'))document.querySelectorAll('.vp').forEach(v=>{v.scrollLeft=(v.scrollWidth-v.clientWidth)/2;v.scrollTop=(v.scrollHeight-v.clientHeight)/2;});};
document.getElementById('flagged').onchange=e=>b.classList.toggle('onlyflagged',e.target.checked);
// keep source and output viewports scrolled to the same spot in crop mode
document.querySelectorAll('.pair').forEach(p=>{const vs=p.querySelectorAll('.vp');let lock=false;
  vs.forEach(v=>v.addEventListener('scroll',()=>{if(lock)return;lock=true;
    vs.forEach(o=>{if(o!==v){o.scrollLeft=v.scrollLeft;o.scrollTop=v.scrollTop;}});
    requestAnimationFrame(()=>lock=false);}));});
"""


def _fmt_bytes(n) -> str:
    return f"{n / 1e6:.2f} MB" if n else "–"


def write_review(parsed: Parsed, cfg: Config, manifest: Manifest, results: dict[str, dict]) -> None:
    tw = cfg.target_width
    e = html.escape
    parts = []
    n_pairs = n_flag = 0
    by_item: dict[str, list[Slot]] = {}
    for s in parsed.slots:
        by_item.setdefault(s.item.slug, []).append(s)
    for item in parsed.items:
        slots = by_item.get(item.slug, [])
        if not slots:
            continue
        parts.append(f'<h2>{e(item.name)} <small>{e(item.slug)}{" · draft" if item.is_draft else ""}</small></h2>')
        for s in slots:
            dl = manifest.get(s.url_key, "download") or {}
            up = manifest.get(s.url_key, "upscale") or {}
            enc = manifest.get(s.url_key, "encode") or {}
            vr = results.get(s.slot_id, {})
            flags = []
            qf = quality_flag(enc)
            for f in filter(None, qf.split(";")):
                flags.append(("warn", f))
            for p in vr.get("problems", []):
                flags.append(("bad", p))
            if not flags and vr.get("ok"):
                flags.append(("ok", "ok"))
            flagged = any(k != "ok" for k, _ in flags)
            n_pairs += 1
            n_flag += flagged
            src_img = f"cache/download/{dl['path']}" if dl.get("path") else ""
            out_img = f"output/{s.out_rel}"
            ftxt = "".join(f'<span class="flag {k}">{e(t)}</span>' for k, t in flags)
            parts.append(f"""
<div class="pair{' flagged' if flagged else ''}" id="{e(s.slot_id)}">
 <div class="title">{e(s.field.label)}{f" #{s.position}/{s.count_in_field}" if s.field.multi else ""}
  <span class="meta">{e(s.slot_id)}</span>{ftxt}</div>
 <div class="grid">
  <div class="cell"><div class="vp"><img loading="lazy" src="{e(src_img)}" alt="source"></div>
   <p class="meta">Source · {dl.get('width', '?')}×{dl.get('height', '?')} px · {_fmt_bytes(dl.get('bytes'))}</p></div>
  <div class="cell"><div class="vp"><img loading="lazy" src="{e(out_img)}" alt="output"></div>
   <p class="meta">Output · {vr.get('width') or '?'}×{vr.get('height') or '?'} px · {_fmt_bytes(vr.get('bytes'))}
   · WebP q{enc.get('quality', '?')} ·{e(up.get('method', '?'))}{f" · tile {up['tile']}" if up.get('tile') else ""}</p></div>
 </div>
</div>""")

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Upscale Review</title><style>{_CSS}</style></head>
<body style="--tw:{tw}px">
<header><h1>Upscale review</h1>
<span class="stats">{n_pairs} slots · {n_flag} flagged · target {tw} px</span>
<button id="crop">100% crop</button>
<label class="tg"><input type="checkbox" id="flagged"> only flagged</label>
<span class="stats">In crop mode both sides are shown at output scale; scroll either one to inspect brickwork, mullions and text.</span>
</header>
<main>{''.join(parts)}</main>
<script>{_JS}</script>
</body></html>"""
    atomic_write_text(cfg.review_html, doc)
    log.info("wrote %s", cfg.review_html)
