"""Load config.toml and resolve project paths."""
from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def find_root(start: Path | None = None) -> Path:
    """Project root = the nearest folder (upwards) containing config.toml.

    Override with the UPSCALE_ROOT environment variable.
    """
    env = os.environ.get("UPSCALE_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    here = (start or Path.cwd()).resolve()
    for p in (here, *here.parents):
        if (p / "config.toml").is_file():
            return p
    # fall back to the repo layout: src/upscale/config.py -> root
    return Path(__file__).resolve().parents[2]


@dataclass
class Config:
    root: Path
    raw: dict[str, Any]

    def _p(self, rel: str) -> Path:
        p = Path(rel).expanduser()
        return p if p.is_absolute() else (self.root / p)

    # paths ---------------------------------------------------------------
    @property
    def input_csv(self) -> Path:
        return self._p(self.raw["paths"]["input_csv"])

    @property
    def originals_dir(self) -> Path:
        return self._p(self.raw["paths"]["originals_override"])

    @property
    def cache_dir(self) -> Path:
        return self._p(self.raw["paths"]["cache"])

    @property
    def download_dir(self) -> Path:
        return self.cache_dir / "download"

    @property
    def prepared_dir(self) -> Path:
        return self.cache_dir / "prepared"

    @property
    def raw_dir(self) -> Path:
        return self.cache_dir / "upscaled_raw"

    @property
    def encoded_dir(self) -> Path:
        return self.cache_dir / "encoded"

    @property
    def manifest_path(self) -> Path:
        return self.cache_dir / "manifest.json"

    @property
    def output_dir(self) -> Path:
        return self._p(self.raw["paths"]["output"])

    @property
    def logs_dir(self) -> Path:
        return self._p(self.raw["paths"]["logs"])

    @property
    def index_csv(self) -> Path:
        return self.root / "index.csv"

    @property
    def index_json(self) -> Path:
        return self.root / "index.json"

    @property
    def review_html(self) -> Path:
        return self.root / "review.html"

    @property
    def runbook(self) -> Path:
        return self.root / "UPLOAD_RUNBOOK.md"

    @property
    def originals_map(self) -> Path:
        return self.root / "originals_map.csv"

    # sections ------------------------------------------------------------
    @property
    def download(self) -> dict[str, Any]:
        return self.raw["download"]

    @property
    def esrgan(self) -> dict[str, Any]:
        d = dict(self.raw["realesrgan"])
        d["binary"] = self._p(d["binary"])
        d["models_dir"] = self._p(d["models_dir"])
        return d

    @property
    def out(self) -> dict[str, Any]:
        return self.raw["output"]

    @property
    def webp(self) -> dict[str, Any]:
        return self.raw["webp"]

    @property
    def cwebp(self) -> Path | None:
        """The installed cwebp: a full path, or a command name looked up on PATH."""
        v = str(self.raw["webp"]["cwebp"])
        if "/" in v:
            p = self._p(v)
            return p if p.is_file() else None
        found = shutil.which(v)
        return Path(found) if found else None

    @property
    def verify(self) -> dict[str, Any]:
        return self.raw["verify"]

    @property
    def target_width(self) -> int:
        return int(self.out["target_width"])


def load_config(root: Path | None = None) -> Config:
    root = root or find_root()
    with open(root / "config.toml", "rb") as f:
        raw = tomllib.load(f)
    return Config(root=root, raw=raw)
