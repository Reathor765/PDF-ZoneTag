"""Template model: page types, identifier rules, and value zones."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

STANDARD_INFO_KEYS = {
    "Title",
    "Author",
    "Subject",
    "Keywords",
    "Creator",
    "Producer",
    "CreationDate",
    "ModDate",
    "Trapped",
}

PDFX_NAMESPACE = "http://ns.adobe.com/pdfx/1.3/"


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def sanitize_tag(name: str) -> str:
    """PDF custom property names used here are a letter plus letters, digits, and underscores."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "", (name or "").strip())
    if not cleaned:
        return ""
    if not cleaned[0].isalpha():
        cleaned = "F" + cleaned
    return cleaned


@dataclass
class RectN:
    """Rectangle as fractions of the displayed page. Origin is the top-left."""

    x: float
    y: float
    w: float
    h: float

    def clamped(self) -> RectN:
        x = min(max(self.x, 0.0), 1.0)
        y = min(max(self.y, 0.0), 1.0)
        w = min(max(self.w, 0.0), 1.0 - x)
        h = min(max(self.h, 0.0), 1.0 - y)
        return RectN(x, y, w, h)

    def padded(self, pad: float) -> RectN:
        return RectN(
            self.x - pad,
            self.y - pad,
            self.w + (2 * pad),
            self.h + (2 * pad),
        ).clamped()

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @classmethod
    def from_dict(cls, data: dict) -> RectN:
        return cls(float(data["x"]), float(data["y"]), float(data["w"]), float(data["h"]))


@dataclass
class Identifier:
    id: str
    scope: str = "region"  # region | page
    op: str = "contains"  # contains | equals | regex
    value: str = ""
    rect: RectN | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "scope": self.scope,
            "op": self.op,
            "value": self.value,
            "rect": None if self.rect is None else self.rect.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Identifier:
        raw = data.get("rect")
        return cls(
            id=data["id"],
            scope=data.get("scope", "region"),
            op=data.get("op", "contains"),
            value=data.get("value", ""),
            rect=None if not raw else RectN.from_dict(raw),
        )


@dataclass
class Zone:
    id: str
    tag: str
    rect: RectN

    def to_dict(self) -> dict:
        return {"id": self.id, "tag": self.tag, "rect": self.rect.to_dict()}

    @classmethod
    def from_dict(cls, data: dict) -> Zone:
        return cls(id=data["id"], tag=data.get("tag", ""), rect=RectN.from_dict(data["rect"]))


@dataclass
class PageType:
    id: str
    name: str
    match: str = "all"  # all | any
    identifiers: list[Identifier] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "match": self.match if self.match in {"all", "any"} else "all",
            "identifiers": [item.to_dict() for item in self.identifiers],
            "zones": [zone.to_dict() for zone in self.zones],
        }

    @classmethod
    def from_dict(cls, data: dict) -> PageType:
        return cls(
            id=data["id"],
            name=data.get("name") or "Page",
            match=data.get("match", "all"),
            identifiers=[Identifier.from_dict(item) for item in data.get("identifiers", [])],
            zones=[Zone.from_dict(zone) for zone in data.get("zones", [])],
        )


@dataclass
class Template:
    name: str = "Untitled"
    pad: float = 0.01
    page_types: list[PageType] = field(default_factory=list)
    version: int = 1

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "name": self.name,
            "pad": self.pad,
            "page_types": [page_type.to_dict() for page_type in self.page_types],
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def from_dict(cls, data: dict) -> Template:
        return cls(
            name=data.get("name") or "Untitled",
            pad=float(data.get("pad", 0.01)),
            page_types=[PageType.from_dict(item) for item in data.get("page_types", [])],
            version=int(data.get("version", 1)),
        )

    @classmethod
    def load(cls, path: str | Path) -> Template:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def page_type(self, type_id: str) -> PageType | None:
        for page_type in self.page_types:
            if page_type.id == type_id:
                return page_type
        return None

    def add_page_type(self, name: str) -> PageType:
        page_type = PageType(id=new_id(), name=name.strip() or "Page")
        self.page_types.append(page_type)
        return page_type

    def find_box(self, box_id: str) -> tuple[PageType, str, Identifier | Zone] | None:
        for page_type in self.page_types:
            for identifier in page_type.identifiers:
                if identifier.id == box_id:
                    return page_type, "identifier", identifier
            for zone in page_type.zones:
                if zone.id == box_id:
                    return page_type, "value", zone
        return None

    def remove_box(self, box_id: str) -> None:
        for page_type in self.page_types:
            page_type.identifiers = [item for item in page_type.identifiers if item.id != box_id]
            page_type.zones = [zone for zone in page_type.zones if zone.id != box_id]
