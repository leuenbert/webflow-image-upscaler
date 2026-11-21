"""Bring back fine surface texture (concrete, plaster, wood) that Real-ESRGAN smooths away.

Real-ESRGAN x4plus is trained to remove noise and JPEG artifacts, so low-contrast texture looks
like noise to it and gets flattened. The ncnn binary has no knob for that (only -x/TTA), so we
post-process the AI result:

  source_blend  mix a plain Lanczos upscale of the *real* source back in (restores genuine texture)
  sharpen       unsharp mask to lift the blended micro-contrast again
  grain         fine monochrome grain, so large flat surfaces don't look plastic
"""
from __future__ import annotations

import random

from PIL import Image, ImageChops, ImageFilter, ImageStat

DEFAULTS = {"source_blend": 0.0, "sharpen": 0, "sharpen_radius": 1.5, "sharpen_threshold": 2,
            "grain": 0.0, "grain_size": 1.4}


def params_from(cfg_section: dict | None) -> dict:
    p = dict(DEFAULTS)
    p.update(cfg_section or {})
    return p


def is_active(p: dict) -> bool:
    return p["source_blend"] > 0 or p["sharpen"] > 0 or p["grain"] > 0


def _grain(size: tuple[int, int], sigma: float, grain_size: float, seed: str) -> Image.Image:
    """Deterministic monochrome noise centred on 128 with std dev `sigma` (8-bit levels)."""
    w, h = size
    gw, gh = max(1, round(w / grain_size)), max(1, round(h / grain_size))
    rng = random.Random(seed)
    noise = Image.frombytes("L", (gw, gh), rng.randbytes(gw * gh)).filter(ImageFilter.GaussianBlur(0.6))
    if (gw, gh) != (w, h):
        noise = noise.resize((w, h), Image.Resampling.BICUBIC)
    st = ImageStat.Stat(noise)
    mean, sd = st.mean[0], max(st.stddev[0], 1e-3)
    k = sigma / sd
    return noise.point(lambda v: max(0, min(255, round(128 + (v - mean) * k))))


def apply(ai: Image.Image, source: Image.Image, p: dict, seed: str = "") -> Image.Image:
    """`ai` is the AI result already at its final size; `source` is the original (any size)."""
    out = ai
    if p["source_blend"] > 0:
        lz = source.resize(ai.size, Image.Resampling.LANCZOS)
        out = Image.blend(out, lz, float(p["source_blend"]))
    if p["sharpen"] > 0:
        out = out.filter(ImageFilter.UnsharpMask(radius=float(p["sharpen_radius"]),
                                                 percent=int(p["sharpen"]),
                                                 threshold=int(p["sharpen_threshold"])))
    if p["grain"] > 0:
        g = _grain(out.size, float(p["grain"]), float(p["grain_size"]), seed)
        out = ImageChops.add(out, Image.merge("RGB", (g, g, g)), scale=1.0, offset=-128)
    return out

