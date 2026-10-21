import json
from pathlib import Path

import pikepdf
import pymupdf

from zonetag.engine import analyze, tags_to_write
from zonetag.model import Identifier, RectN, Template, Zone, new_id
from zonetag.ocr import recognize
from zonetag.pdfio import norm_to_rect, page_pixmap, pixmap_to_bgr, read_custom, write_tags


def _norm_for(page, words: str, pad: float = 0.01) -> RectN:
    hits = page.search_for(words)
    assert hits, words
    rect = hits[0]
    box = page.rect
    found = RectN(
        (rect.x0 - box.x0) / box.width,
        (rect.y0 - box.y0) / box.height,
        rect.width / box.width,
        rect.height / box.height,
    )
    return found.padded(pad)


def _write_pages(path: Path, pages: list[list[tuple[str, float, float]]]) -> None:
    document = pymupdf.open()
    for lines in pages:
        page = document.new_page(width=612, height=792)
        for text, x, y in lines:
            page.insert_text((x, y), text, fontsize=14)
    document.save(path)
    document.close()


def _template_for(sample: Path) -> Template:
    document = pymupdf.open(sample)
    declaration, terms, grocery = document[0], document[1], document[2]
    template = Template(name="VSC", pad=0.005)
    cover = template.add_page_type("Declaration")
    cover.identifiers.append(
        Identifier(id=new_id(), op="contains", value="VEHICLE SERVICE CONTRACT", rect=_norm_for(declaration, "VEHICLE SERVICE CONTRACT"))
    )
    cover.zones.append(Zone(id=new_id(), tag="ContractNumber", rect=_norm_for(declaration, "VSC-10042")))
    rules = template.add_page_type("Terms")
    rules.match = "any"
    rules.identifiers.append(
        Identifier(id=new_id(), scope="page", op="contains", value="TERMS AND CONDITIONS", rect=None)
    )
    rules.zones.append(Zone(id=new_id(), tag="TermMonths", rect=_norm_for(terms, "24")))
    document.close()
    return template


def _sample(path: Path) -> None:
    _write_pages(
        path,
        [
            [("VEHICLE SERVICE CONTRACT", 48, 72), ("VSC-10042", 400, 72)],
            [("TERMS AND CONDITIONS", 48, 72), ("24", 400, 140)],
            [("GROCERY LIST", 48, 72), ("Milk", 48, 110)],
        ],
    )


def test_pages_match_by_content_and_skip_unknown(tmp_path: Path):
    sample = tmp_path / "ordered.pdf"
    _sample(sample)
    shuffled = tmp_path / "shuffled.pdf"
    _write_pages(
        shuffled,
        [
            [("GROCERY LIST", 48, 72), ("Milk", 48, 110)],
            [("TERMS AND CONDITIONS", 48, 72), ("24", 400, 140)],
            [("VEHICLE SERVICE CONTRACT", 48, 72), ("VSC-10042", 400, 72)],
        ],
    )
    template = _template_for(sample)
    plan = analyze(shuffled, template, allow_ocr=False)
    assert [page.status for page in plan.pages] == ["skipped", "matched", "matched"]
    assert plan.pages[1].type_names == ["Terms"]
    assert plan.pages[2].type_names == ["Declaration"]
    values = {tag.tag: tag.new_value for tag in plan.tags}
    assert values["ContractNumber"] == "VSC-10042"
    assert values["TermMonths"] == "24"
    assert plan.status == "ready"
    assert tags_to_write(plan) == values


def test_existing_tag_is_kept_until_overwrite(tmp_path: Path):
    sample = tmp_path / "form.pdf"
    _sample(sample)
    template = _template_for(sample)
    tagged = tmp_path / "tagged.pdf"
    write_tags(sample, tagged, {"ContractNumber": "OLD-1"})
    plan = analyze(tagged, template, allow_ocr=False)
    contract = next(tag for tag in plan.tags if tag.tag == "ContractNumber")
    months = next(tag for tag in plan.tags if tag.tag == "TermMonths")
    assert contract.status == "conflict"
    assert contract.decision == "skip"
    assert months.status == "new"
    assert tags_to_write(plan) == {"TermMonths": "24"}
    kept = tmp_path / "kept.pdf"
    write_tags(tagged, kept, tags_to_write(plan))
    assert read_custom(kept)["ContractNumber"] == "OLD-1"
    assert read_custom(kept)["TermMonths"] == "24"

    contract.decision = "write"
    contract.write_value = contract.new_value
    replaced = tmp_path / "replaced.pdf"
    write_tags(tagged, replaced, tags_to_write(plan))
    assert read_custom(replaced)["ContractNumber"] == "VSC-10042"


def test_same_value_is_unchanged(tmp_path: Path):
    sample = tmp_path / "form.pdf"
    _sample(sample)
    template = _template_for(sample)
    tagged = tmp_path / "tagged.pdf"
    write_tags(sample, tagged, {"ContractNumber": "VSC-10042"})
    plan = analyze(tagged, template, allow_ocr=False)
    contract = next(tag for tag in plan.tags if tag.tag == "ContractNumber")
    assert contract.status == "unchanged"
    assert "ContractNumber" not in tags_to_write(plan)


def test_double_match_is_not_assigned(tmp_path: Path):
    sample = tmp_path / "form.pdf"
    _sample(sample)
    template = _template_for(sample)
    template.page_types[1].identifiers[0].value = "VEHICLE SERVICE CONTRACT"
    template.page_types[1].identifiers[0].scope = "page"
    plan = analyze(sample, template, allow_ocr=False)
    assert plan.pages[0].status == "ambiguous"
    assert plan.status == "conflict"
    assert "ContractNumber" not in {tag.tag for tag in plan.tags}


def test_signed_pdf_is_not_writable(tmp_path: Path):
    sample = tmp_path / "form.pdf"
    _sample(sample)
    with pikepdf.open(sample, allow_overwriting_input=True) as pdf:
        field = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Annot,
                Subtype=pikepdf.Name.Widget,
                FT=pikepdf.Name.Sig,
                Rect=pikepdf.Array([0, 0, 10, 10]),
                T="Signature1",
            )
        )
        pdf.pages[0].Annots = pikepdf.Array([field])
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([field]), SigFlags=3)
        pdf.save(sample)
    template = _template_for(sample)
    plan = analyze(sample, template, allow_ocr=False)
    assert plan.status == "signed"
    assert plan.writable is False
    assert tags_to_write(plan) == {}


def test_template_roundtrip(tmp_path: Path):
    sample = tmp_path / "form.pdf"
    _sample(sample)
    template = _template_for(sample)
    path = tmp_path / "form.zonetag.json"
    template.save(path)
    loaded = Template.load(path)
    assert loaded.page_types[0].zones[0].tag == "ContractNumber"
    assert loaded.page_types[1].identifiers[0].scope == "page"
    json.loads(path.read_text(encoding="utf-8"))


def test_metadata_visible_as_info_and_xmp(tmp_path: Path):
    sample = tmp_path / "form.pdf"
    _sample(sample)
    dest = tmp_path / "out.pdf"
    write_tags(sample, dest, {"ContractNumber": "VSC-10042"})
    with pikepdf.open(dest) as pdf:
        assert str(pdf.docinfo["/ContractNumber"]) == "VSC-10042"
        with pdf.open_metadata() as meta:
            packet = str(meta)
    assert "VSC-10042" in packet
    assert "pdfx" in packet or "ns.adobe.com/pdfx" in packet


def test_ocr_reads_painted_text(tmp_path: Path):
    path = tmp_path / "paint.pdf"
    document = pymupdf.open()
    page = document.new_page(width=400, height=200)
    page.insert_text((40, 80), "HELLO", fontsize=28)
    pix = page_pixmap(page, 2)
    document.close()
    text = recognize(pixmap_to_bgr(pix))
    assert "HELLO" in text.upper()


def test_region_clip_covers_the_word(tmp_path: Path):
    path = tmp_path / "form.pdf"
    _sample(path)
    document = pymupdf.open(path)
    page = document[0]
    rect = _norm_for(page, "VSC-10042", pad=0)
    clip = norm_to_rect(page, rect)
    assert "VSC-10042" in page.get_text("text", clip=clip)
    document.close()
