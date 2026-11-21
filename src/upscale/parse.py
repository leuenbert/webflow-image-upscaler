"""Stage 1: turn the Webflow CSV export into upload slots and a unique-URL table."""
from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .common import sha1_text


OUTPUT_EXT = ".webp"  # final upload format


@dataclass(frozen=True)
class FieldDef:
    label: str        # exact Webflow CMS label / CSV column
    key: str          # short key used in slot ids and filenames
    multi: bool


# Field order inside an item: Main, Secondary, Gallery 1..n, Work 1..3
FIELDS: tuple[FieldDef, ...] = (
    FieldDef("Main Image", "main", False),
    FieldDef("Secondary Image", "secondary", False),
    FieldDef("Gallery", "gallery", True),
    FieldDef("Work — image #1", "work-1", False),
    FieldDef("Work — image #2", "work-2", False),
    FieldDef("Work — image #3", "work-3", False),
)


@dataclass
class Item:
    order: int
    name: str
    slug: str
    item_id: str
    collection_id: str
    is_draft: bool


@dataclass
class Slot:
    item: Item
    field: FieldDef
    position: int
    count_in_field: int
    source_url: str

    @property
    def url_key(self) -> str:
        return sha1_text(self.source_url)

    @property
    def slot_id(self) -> str:
        return f"{self.item.slug}:{self.field.key}:{self.position:02d}"

    @property
    def filename(self) -> str:
        base = f"{ascii_slug(self.item.slug)}--{self.field.key}"
        if self.field.multi:
            base += f"-{self.position:02d}"
        return base + OUTPUT_EXT

    @property
    def out_rel(self) -> str:
        """Output path relative to the output folder: <slug>/<slug>--<field>[-NN].webp"""
        return f"{ascii_slug(self.item.slug)}/{self.filename}"


@dataclass
class Source:
    url: str
    key: str
    slots: list[Slot] = field(default_factory=list)

    @property
    def stem(self) -> str:
        return original_stem(self.url)


@dataclass
class Parsed:
    items: list[Item]
    slots: list[Slot]
    sources: dict[str, Source]   # url_key -> Source, in first-seen order


def _norm_col(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).strip().casefold()
    return re.sub(r"[‐-―\-]+", "-", s)  # all dash variants -> "-"


def ascii_slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "item"


_WF_PREFIX = re.compile(r"^(?:[0-9a-f]{24}_)+", re.I)
_HASH_SUFFIX = re.compile(r"(?:-[0-9a-f]{8})+$", re.I)


def original_stem(url: str) -> str:
    """Recover the photographer's filename stem from a Webflow CDN URL.

    .../6a66..._690b..._IMG_7041-602c2e6f-924f645a%25401120ll.jpeg -> IMG_7041
    """
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    name = _WF_PREFIX.sub("", name)
    for _ in range(3):  # %2540 is a double-encoded "@"
        dec = unquote(name)
        if dec == name:
            break
        name = dec
    stem = name.rsplit(".", 1)[0]
    stem = stem.split("@", 1)[0]          # drop "@1120ll" resize marker
    stem = _HASH_SUFFIX.sub("", stem)     # drop "-602c2e6f-924f645a" hashes
    return stem


def _split_multi(value: str) -> list[str]:
    return [p.strip() for p in value.split(";") if p.strip()]


def parse_csv(path: Path) -> Parsed:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        columns = reader.fieldnames or []

    colmap = {_norm_col(c): c for c in columns}
    resolved: list[tuple[FieldDef, str | None]] = [(fd, colmap.get(_norm_col(fd.label))) for fd in FIELDS]

    items: list[Item] = []
    slots: list[Slot] = []
    sources: dict[str, Source] = {}

    for i, row in enumerate(rows):
        item = Item(
            order=i,
            name=row["Name"].strip(),
            slug=row["Slug"].strip(),
            item_id=row["Item ID"].strip(),
            collection_id=row["Collection ID"].strip(),
            is_draft=row.get("Draft", "").strip().lower() == "true",
        )
        items.append(item)
        for fd, col in resolved:
            if not col:
                continue
            value = (row.get(col) or "").strip()
            if not value:
                continue
            urls = _split_multi(value) if fd.multi else [value]
            for pos, url in enumerate(urls, start=1):
                slot = Slot(item, fd, pos, len(urls), url)
                slots.append(slot)
                src = sources.get(slot.url_key)
                if src is None:
                    src = sources[slot.url_key] = Source(url=url, key=slot.url_key)
                src.slots.append(slot)

    return Parsed(items=items, slots=slots, sources=sources)


def select(parsed: Parsed, only_slugs: list[str] | None, limit: int | None) -> Parsed:
    """Restrict to some items (by slug and/or the first N items)."""
    items = parsed.items
    if only_slugs:
        wanted = set(only_slugs)
        missing = wanted - {it.slug for it in items}
        if missing:
            raise SystemExit(f"Unknown slug(s): {', '.join(sorted(missing))}")
        items = [it for it in items if it.slug in wanted]
    if limit is not None:
        items = items[:limit]
    keep = {it.slug for it in items}
    slots = [s for s in parsed.slots if s.item.slug in keep]
    sources: dict[str, Source] = {}
    for s in slots:
        sources.setdefault(s.url_key, parsed.sources[s.url_key])
    return Parsed(items=items, slots=slots, sources=sources)
