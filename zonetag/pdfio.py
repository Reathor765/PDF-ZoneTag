"""PDF rendering, region text, and custom metadata."""

from __future__ import annotations

import re
from pathlib import Path

import pikepdf
import pymupdf
from pikepdf.models.metadata import PdfMetadata

from zonetag.model import PDFX_NAMESPACE, STANDARD_INFO_KEYS, RectN, sanitize_tag
from zonetag.ocr import pixmap_to_bgr, recognize

_WHITESPACE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    return _WHITESPACE.sub(" ", value or "").strip()


def open_document(path: str | Path):
    return pymupdf.open(path)


def page_pixmap(page, zoom: float, clip: pymupdf.Rect | None = None):
    matrix = pymupdf.Matrix(zoom, zoom)
    return page.get_pixmap(matrix=matrix, clip=clip, alpha=False, colorspace=pymupdf.csRGB)


def render_png(page, zoom: float) -> bytes:
    return page_pixmap(page, zoom).tobytes("png")


def norm_to_rect(page, rect: RectN) -> pymupdf.Rect:
    box = page.rect
    return pymupdf.Rect(
        box.x0 + (rect.x * box.width),
        box.y0 + (rect.y * box.height),
        box.x0 + ((rect.x + rect.w) * box.width),
        box.y0 + ((rect.y + rect.h) * box.height),
    )


def rect_from_points(page, rect: pymupdf.Rect) -> RectN:
    box = page.rect
    return RectN(
        (rect.x0 - box.x0) / box.width,
        (rect.y0 - box.y0) / box.height,
        rect.width / box.width,
        rect.height / box.height,
    ).clamped()


def extract_text(page, rect: RectN | None) -> str:
    if rect is None:
        raw = page.get_text("text")
    else:
        raw = page.get_text("text", clip=norm_to_rect(page, rect))
    return normalize_text(raw)


def ocr_region(page, rect: RectN | None, zoom: float = 3.0) -> str:
    clip = None if rect is None else norm_to_rect(page, rect)
    pix = page_pixmap(page, zoom, clip=clip)
    if pix.width < 2 or pix.height < 2:
        return ""
    return normalize_text(recognize(pixmap_to_bgr(pix)))


def read_region(page, rect: RectN | None, *, pad: float = 0.0, allow_ocr: bool = True) -> tuple[str, str]:
    """Return `(text, source)` where source is `text`, `ocr`, or `empty`."""
    target = rect
    if rect is not None and pad:
        target = rect.padded(pad)
    text = extract_text(page, target)
    if text:
        return text, "text"
    if not allow_ocr:
        return "", "empty"
    text = ocr_region(page, target)
    if text:
        return text, "ocr"
    return "", "empty"


def document_guard(path: str | Path) -> str | None:
    """Return `encrypted`, `signed`, or None when the file can be tagged."""
    try:
        pdf = pikepdf.open(path)
    except pikepdf.PasswordError:
        return "encrypted"
    try:
        if pdf.is_encrypted:
            return "encrypted"
        if _is_signed(pdf):
            return "signed"
        return None
    finally:
        pdf.close()


def _is_signed(pdf: pikepdf.Pdf) -> bool:
    root = pdf.Root
    if "/Perms" in root:
        return True
    acro = root.get("/AcroForm")
    if acro is not None:
        for field in acro.get("/Fields", []):
            if str(field.get("/FT", "")) == "/Sig":
                return True
    for page in pdf.pages:
        for annot in page.get("/Annots") or []:
            if str(annot.get("/FT", "")) == "/Sig":
                return True
    return False


def _meta_name(key: str) -> str:
    if key.startswith("{"):
        return key.split("}", 1)[-1]
    if ":" in key:
        return key.split(":", 1)[1]
    return key


def _as_text(value) -> str:
    if isinstance(value, list):
        return normalize_text(str(value[0])) if value else ""
    return normalize_text(str(value))


def read_custom(path: str | Path) -> dict[str, str]:
    """Custom tags from the Info dictionary, filled in with pdfx XMP when Info is missing."""
    found: dict[str, str] = {}
    with pikepdf.open(path) as pdf:
        PdfMetadata.register_xml_namespace(PDFX_NAMESPACE, "pdfx")
        with pdf.open_metadata() as meta:
            for key, value in meta.items():
                key_text = str(key)
                if PDFX_NAMESPACE not in key_text and not key_text.startswith("pdfx:"):
                    continue
                name = sanitize_tag(_meta_name(key_text))
                if name:
                    found[name] = _as_text(value)
        info = pdf.docinfo
        if info:
            for key, value in info.items():
                name = sanitize_tag(str(key).lstrip("/"))
                if not name or name in STANDARD_INFO_KEYS:
                    continue
                found[name] = _as_text(value)
    return found


def write_tags(source: str | Path, dest: str | Path, tags: dict[str, str]) -> None:
    """Write a copy of `source` with custom Info keys and matching pdfx XMP."""
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    clean = {sanitize_tag(name): value for name, value in tags.items()}
    clean = {name: value for name, value in clean.items() if name and value}
    if not clean:
        raise ValueError("There are no tags to write.")
    with pikepdf.open(source) as pdf:
        if pdf.is_encrypted:
            raise PermissionError("This PDF is encrypted.")
        if _is_signed(pdf):
            raise PermissionError("This PDF is signed. ZoneTag will not modify it.")
        PdfMetadata.register_xml_namespace(PDFX_NAMESPACE, "pdfx")
        with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as meta:
            for name, value in clean.items():
                meta[f"pdfx:{name}"] = value
                pdf.docinfo[f"/{name}"] = value
        pdf.save(dest_path)


def unique_dest(folder: str | Path, filename: str) -> Path:
    directory = Path(folder)
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    number = 2
    while True:
        candidate = directory / f"{stem}_{number}{suffix}"
        if not candidate.exists():
            return candidate
        number += 1
