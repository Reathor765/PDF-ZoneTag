"""Match pages to a template and plan metadata writes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from zonetag.model import Identifier, PageType, Template
from zonetag.pdfio import document_guard, extract_text, normalize_text, ocr_region, read_custom


@dataclass
class PageReport:
    index: int
    status: str  # matched | skipped | ambiguous
    type_ids: list[str] = field(default_factory=list)
    type_names: list[str] = field(default_factory=list)
    readings: dict[str, str] = field(default_factory=dict)
    note: str = ""


@dataclass
class TagProposal:
    tag: str
    new_value: str
    existing: str | None
    status: str  # new | unchanged | conflict | ambiguous
    decision: str = "skip"  # skip | write
    write_value: str = ""
    note: str = ""
    options: list[tuple[str, str, str]] = field(default_factory=list)
    """Each option is (label, decision, write_value)."""


@dataclass
class FilePlan:
    path: str
    status: str
    message: str = ""
    pages: list[PageReport] = field(default_factory=list)
    tags: list[TagProposal] = field(default_factory=list)
    writable: bool = False


def compare_text(text: str, op: str, expected: str) -> bool:
    if not (expected or "").strip():
        return False
    left = normalize_text(text)
    if op == "equals":
        return left.casefold() == normalize_text(expected).casefold()
    if op == "regex":
        try:
            return re.search(expected, left, re.IGNORECASE) is not None
        except re.error:
            return False
    return normalize_text(expected).casefold() in left.casefold()


class _Reader:
    def __init__(self, page, allow_ocr: bool):
        self.page = page
        self.allow_ocr = allow_ocr
        self._full: str | None = None

    def text(self, rect, pad: float) -> str:
        target = None if rect is None else (rect.padded(pad) if pad else rect)
        found = extract_text(self.page, target)
        if found or not self.allow_ocr:
            return found
        return ocr_region(self.page, target)

    def full(self) -> str:
        if self._full is None:
            self._full = self.text(None, 0.0)
        return self._full


def _identifier_hits(reader: _Reader, identifier: Identifier) -> bool:
    if identifier.scope == "page" or identifier.rect is None:
        found = reader.full()
    else:
        found = reader.text(identifier.rect, 0.0)
    return compare_text(found, identifier.op, identifier.value)


def matching_types(reader: _Reader, template: Template) -> list[PageType]:
    hits: list[PageType] = []
    for page_type in template.page_types:
        if not page_type.identifiers:
            continue
        results = [_identifier_hits(reader, identifier) for identifier in page_type.identifiers]
        matched = all(results) if page_type.match != "any" else any(results)
        if matched:
            hits.append(page_type)
    return hits


def _proposal_for(tag: str, values: list[str], existing: str | None) -> TagProposal | None:
    unique = list(dict.fromkeys(value for value in values if value))
    if not unique:
        return None
    if len(unique) > 1:
        options = [("Skip this tag", "skip", "")]
        options.extend((f"Use {value}", "write", value) for value in unique)
        return TagProposal(
            tag=tag,
            new_value=unique[-1],
            existing=existing,
            status="ambiguous",
            decision="skip",
            write_value="",
            note="Zones disagree: " + " | ".join(unique),
            options=options,
        )
    value = unique[0]
    if existing is None:
        return TagProposal(
            tag=tag,
            new_value=value,
            existing=None,
            status="new",
            decision="write",
            write_value=value,
            note="New tag",
        )
    if normalize_text(existing) == value:
        return TagProposal(
            tag=tag,
            new_value=value,
            existing=existing,
            status="unchanged",
            decision="skip",
            write_value="",
            note="Already set to this value",
        )
    return TagProposal(
        tag=tag,
        new_value=value,
        existing=existing,
        status="conflict",
        decision="skip",
        write_value="",
        note="A different value is already on the file",
        options=[
            ("Keep existing", "skip", ""),
            (f"Overwrite with {value}", "write", value),
        ],
    )


def analyze(path: str | Path, template: Template, *, allow_ocr: bool = True) -> FilePlan:
    source = Path(path)
    plan = FilePlan(path=str(source), status="error")
    guard = document_guard(source)
    if guard == "encrypted":
        plan.status = "encrypted"
        plan.message = "Encrypted PDF. Skipped."
        return plan
    try:
        document = pymupdf.open(source)
    except Exception as exc:  # noqa: BLE001 - report per file, keep the batch going
        plan.status = "error"
        plan.message = str(exc)
        return plan
    try:
        existing = {} if guard == "signed" else read_custom(source)
        bag: dict[str, list[str]] = {}
        for index in range(document.page_count):
            page = document[index]
            reader = _Reader(page, allow_ocr)
            hits = matching_types(reader, template)
            if not hits:
                plan.pages.append(PageReport(index=index, status="skipped", note="No page rule matched"))
                continue
            if len(hits) > 1:
                plan.pages.append(
                    PageReport(
                        index=index,
                        status="ambiguous",
                        type_ids=[item.id for item in hits],
                        type_names=[item.name for item in hits],
                        note="Matches more than one page type",
                    )
                )
                continue
            page_type = hits[0]
            readings: dict[str, str] = {}
            for zone in page_type.zones:
                tag = zone.tag.strip()
                if not tag:
                    continue
                found = reader.text(zone.rect, template.pad)
                readings[tag] = found
                bag.setdefault(tag, []).append(found)
            empty = [tag for tag, value in readings.items() if not value]
            plan.pages.append(
                PageReport(
                    index=index,
                    status="matched",
                    type_ids=[page_type.id],
                    type_names=[page_type.name],
                    readings=readings,
                    note=("Empty: " + ", ".join(empty)) if empty else "",
                )
            )
        for tag, values in bag.items():
            proposal = _proposal_for(tag, values, existing.get(tag))
            if proposal is not None:
                plan.tags.append(proposal)
    except Exception as exc:  # noqa: BLE001
        plan.status = "error"
        plan.message = str(exc)
        return plan
    finally:
        document.close()

    matched_ids = {type_id for page in plan.pages for type_id in page.type_ids if page.status == "matched"}
    skipped = sum(1 for page in plan.pages if page.status == "skipped")
    matched = sum(1 for page in plan.pages if page.status == "matched")
    if any(page.status == "ambiguous" for page in plan.pages) or any(
        tag.status in {"conflict", "ambiguous"} for tag in plan.tags
    ):
        plan.status = "conflict"
    elif not matched_ids:
        plan.status = "skipped"
    elif len(matched_ids) < len([item for item in template.page_types if item.identifiers]):
        plan.status = "partial"
    else:
        plan.status = "ready"
    plan.message = f"{matched} matched, {skipped} skipped, {len(plan.tags)} tag(s)"
    plan.writable = plan.status in {"ready", "partial", "conflict"}
    if guard == "signed":
        plan.status = "signed"
        plan.writable = False
        plan.message = "Signed PDF, left unchanged. " + plan.message
    return plan


def tags_to_write(plan: FilePlan) -> dict[str, str]:
    if not plan.writable:
        return {}
    chosen: dict[str, str] = {}
    for tag in plan.tags:
        if tag.decision == "write" and tag.write_value:
            chosen[tag.tag] = tag.write_value
    return chosen
