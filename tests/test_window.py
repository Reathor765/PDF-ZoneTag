import os
from pathlib import Path

import pymupdf  # type: ignore[import-not-found]
import pytest  # type: ignore[import-not-found]

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # type: ignore[import-not-found]
from PySide6.QtGui import QAction  # type: ignore[import-not-found]
from PySide6.QtTest import QTest  # type: ignore[import-not-found]
from PySide6.QtWidgets import (  # type: ignore[import-not-found]
    QApplication,
    QComboBox,
    QDockWidget,
    QGraphicsView,
    QLineEdit,
    QListWidget,
    QTableWidget,
)

from zonetag.model import RectN
from zonetag.window import MainController


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_designer_window_loads(qapp):
    controller = MainController()
    page_view = controller.ui.findChild(QGraphicsView, "pageView")
    assert page_view is not None
    assert page_view.objectName() == "pageView"
    assert controller.w(QDockWidget, "dockInspector").windowTitle() == "Inspector"
    assert controller.w(QDockWidget, "dockFiles").windowTitle() == "Files"
    assert controller.w(QDockWidget, "dockResults").windowTitle() == "Results"
    assert controller.template.page_types[0].name == "Page A"
    assert not controller.ui.windowIcon().isNull()
    controller.shutdown()
    controller.ui.close()


def test_drawn_box_becomes_a_value_zone(qapp):
    controller = MainController()
    controller.w(QLineEdit, "tagEdit").setText("VIN")
    controller.w(QComboBox, "roleCombo").setCurrentIndex(0)
    controller.add_drawn_box(0.1, 0.2, 0.3, 0.05)
    zone = controller.template.page_types[0].zones[0]
    assert zone.tag == "VIN"
    assert zone.rect == RectN(0.1, 0.2, 0.3, 0.05)
    controller.canvas.zoneDeleteRequested.emit()
    assert controller.template.page_types[0].zones == []
    controller.shutdown()
    controller.ui.close()


def _norm(page, words: str) -> RectN:
    hit = page.search_for(words)[0]
    box = page.rect
    return RectN(
        (hit.x0 - box.x0) / box.width,
        (hit.y0 - box.y0) / box.height,
        hit.width / box.width,
        hit.height / box.height,
    ).padded(0.01)


def test_second_page_keeps_the_zones(qapp, tmp_path: Path):
    path = tmp_path / "two.pdf"
    document = pymupdf.open()
    document.new_page(width=612, height=792)
    document.new_page(width=612, height=792)
    document.save(path)
    document.close()
    controller = MainController()
    controller.ui.resize(1000, 800)
    controller.ui.show()
    qapp.processEvents()
    controller.open_pdf(path)
    controller.add_drawn_box(0.1, 0.1, 0.2, 0.08)
    strip = controller.w(QListWidget, "filmstrip")
    item = strip.item(1)
    assert item is not None
    rect = strip.visualItemRect(item)
    QTest.mouseClick(strip.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
    qapp.processEvents()
    assert controller.page_index == 1
    assert len(controller.template.page_types[0].zones) == 1
    controller.shutdown()
    controller.ui.close()


def test_drag_on_page_creates_a_zone(qapp, tmp_path: Path):
    path = tmp_path / "blank.pdf"
    document = pymupdf.open()
    document.new_page(width=612, height=792)
    document.save(path)
    document.close()
    controller = MainController()
    controller.ui.resize(1000, 800)
    controller.ui.show()
    qapp.processEvents()
    controller.open_pdf(path)
    qapp.processEvents()
    viewport = controller.canvas.view.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(30, 30))
    QTest.mouseMove(viewport, QPoint(180, 100))
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(180, 100))
    qapp.processEvents()
    assert len(controller.template.page_types[0].zones) == 1
    controller.shutdown()
    controller.ui.close()


def test_open_pdf_and_test_this_file(qapp, tmp_path: Path):
    path = tmp_path / "sample.pdf"
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((48, 72), "VEHICLE SERVICE CONTRACT", fontsize=16)
    page.insert_text((400, 72), "VSC-10042", fontsize=16)
    ident = _norm(page, "VEHICLE SERVICE CONTRACT")
    value = _norm(page, "VSC-10042")
    document.save(path)
    document.close()

    controller = MainController()
    controller.ui.resize(1000, 800)
    controller.ui.show()
    controller.open_pdf(path)
    assert controller.w(QListWidget, "filmstrip").count() == 1

    controller.w(QLineEdit, "tagEdit").setText("ContractNumber")
    controller.add_drawn_box(value.x, value.y, value.w, value.h)
    controller.selected_box = ""
    controller._load_inspector("")
    controller.w(QComboBox, "roleCombo").setCurrentIndex(1)
    controller.add_drawn_box(ident.x, ident.y, ident.w, ident.h)
    controller.template.page_types[0].identifiers[0].value = "VEHICLE SERVICE CONTRACT"
    controller.test_current()

    assert controller.page_reports[0].status == "matched"
    table = controller.w(QTableWidget, "tagTable")
    tag_item = table.item(0, 1)
    value_item = table.item(0, 2)
    assert tag_item is not None and tag_item.text() == "ContractNumber"
    assert value_item is not None and value_item.text() == "VSC-10042"
    controller.shutdown()
    controller.ui.close()


def test_folder_preview_fills_the_batch_list(qapp, tmp_path: Path):
    path = tmp_path / "scan.pdf"
    document = pymupdf.open()
    document.new_page(width=612, height=792)
    document.save(path)
    document.close()
    controller = MainController()
    controller.batch_dir = tmp_path
    controller.output_dir = tmp_path / "ZoneTag-output"
    controller.start_preview()
    for _ in range(40):
        worker = controller.worker
        if controller.plans and worker is not None and not worker.isRunning():
            break
        QTest.qWait(50)
    table = controller.w(QTableWidget, "batchTable")
    item = table.item(0, 0)
    assert item is not None and item.text() == "scan.pdf"
    assert controller.w(QAction, "actionApply").isEnabled()
    controller.shutdown()
    controller.ui.close()
