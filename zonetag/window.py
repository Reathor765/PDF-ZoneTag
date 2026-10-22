"""ZoneTag main window. Layout comes from ui/mainwindow.ui."""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar, cast

import pymupdf  # type: ignore[import-not-found]
from PySide6.QtCore import QFile, QObject, Qt, QThread, QTimer, Signal  # type: ignore[import-not-found]
from PySide6.QtGui import QAction, QColor, QGuiApplication, QIcon, QImage, QPalette, QPixmap  # type: ignore[import-not-found]
from PySide6.QtUiTools import QUiLoader  # type: ignore[import-not-found]
from PySide6.QtWidgets import (  # type: ignore[import-not-found]
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QGraphicsView,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)  

from zonetag.canvas import PageCanvas
from zonetag.engine import FilePlan, PageReport, analyze, tags_to_write
from zonetag.model import Identifier, RectN, Template, Zone, new_id, sanitize_tag
from zonetag.ocr import ocr_error
from zonetag.pdfio import read_region, render_png, unique_dest, write_tags

_WidgetT = TypeVar("_WidgetT", bound=QObject)


def png_image(data: bytes) -> QImage:
    # The stub types the format as bytes. PySide accepts the format name as a string.
    return QImage.fromData(data, cast(bytes, "PNG"))


UI_PATH = Path(__file__).resolve().parent.parent / "ui" / "mainwindow.ui"
ICON_PATH = Path(__file__).resolve().parent / "Asset-1.svg"

PAGE_COLORS = {
    "matched": "#1f6b45",
    "skipped": "#3a414b",
    "ambiguous": "#8a5a12",
}
STATUS_COLORS = {
    "ready": "#1f6b45",
    "partial": "#3d5a8a",
    "conflict": "#8a5a12",
    "skipped": "#3a414b",
    "signed": "#6b3a4a",
    "encrypted": "#6b3a4a",
    "error": "#8a3030",
    "unchanged": "#3a414b",
}


def apply_editor_palette(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    base = QColor("#2c313a")
    panel = QColor("#23282f")
    alt = QColor("#343b46")
    text = QColor("#e7e7e7")
    disabled = QColor("#8b93a0")
    highlight = QColor("#3d8bfd")
    palette.setColor(QPalette.ColorRole.Window, base)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, panel)
    palette.setColor(QPalette.ColorRole.AlternateBase, alt)
    palette.setColor(QPalette.ColorRole.ToolTipBase, panel)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, alt)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.Highlight, highlight)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled)
    app.setPalette(palette)


_UI_LOADER: QUiLoader | None = None


def load_main_window() -> QMainWindow:
    global _UI_LOADER
    loader = QUiLoader()
    _UI_LOADER = loader
    handle = QFile(str(UI_PATH))
    if not handle.open(QFile.OpenModeFlag.ReadOnly):
        raise RuntimeError(handle.errorString())
    window = loader.load(handle)
    handle.close()
    if not isinstance(window, QMainWindow):
        raise RuntimeError(loader.errorString() or f"Could not load {UI_PATH}")
    return window


class PreviewWorker(QThread):
    file_done = Signal(object)
    finished_ok = Signal()

    def __init__(self, paths: list[Path], template: Template):
        super().__init__()
        self.paths = paths
        self.template = template

    def run(self) -> None:
        for path in self.paths:
            if self.isInterruptionRequested():
                break
            self.file_done.emit(analyze(path, self.template))
        self.finished_ok.emit()


class MainController:
    def __init__(self):
        self.ui = load_main_window()
        self.ui.setWindowIcon(QIcon(str(ICON_PATH)))
        self.template = Template()
        self.template.add_page_type("Page A")
        self.template_path: Path | None = None
        self.doc: pymupdf.Document | None = None
        self.doc_path: Path | None = None
        self.page_index = 0
        self.page_reports: list[PageReport] = []
        self.plans: list[FilePlan] = []
        self.batch_dir: Path | None = None
        self.output_dir: Path | None = None
        self.selected_box = ""
        self._loading = False
        self._refreshing = False
        self._page_change_pending = False
        self.worker: PreviewWorker | None = None

        page_view = self.ui.findChild(QGraphicsView, "pageView")
        if page_view is None:
            raise RuntimeError("Missing widget pageView")
        self.canvas = PageCanvas(page_view)
        self._prepare_tables()
        self._connect()
        self._refresh_page_types()
        self._set_inspector_enabled(False)
        self.ui.statusBar().showMessage("Open a sample PDF, draw identifier boxes, then draw the value boxes.")
        self.log("Draw on the page. Identifier boxes decide which page type a scan is. Value boxes become metadata tags.")

    def w(self, kind: type[_WidgetT], name: str) -> _WidgetT:
        found = self.ui.findChild(kind, name)
        if found is None:
            raise RuntimeError(f"Missing widget {name}")
        return found

    def _prepare_tables(self) -> None:
        batch = self.w(QTableWidget, "batchTable")
        batch.setColumnCount(3)
        batch.setHorizontalHeaderLabels(["File", "Status", "Summary"])
        batch.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        batch.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        batch.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        batch.verticalHeader().setVisible(False)

        tags = self.w(QTableWidget, "tagTable")
        tags.setColumnCount(4)
        tags.setHorizontalHeaderLabels(["File", "Tag", "Value", "Status"])
        tags.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        tags.verticalHeader().setVisible(False)

        conflicts = self.w(QTableWidget, "conflictTable")
        conflicts.setColumnCount(6)
        conflicts.setHorizontalHeaderLabels(["File", "Tag", "On file", "New value", "Choice", "Note"])
        conflicts.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        conflicts.verticalHeader().setVisible(False)

    def _connect(self) -> None:
        self.w(QAction, "actionOpen").triggered.connect(self.open_pdf_dialog)
        self.w(QAction, "actionOpenTemplate").triggered.connect(self.open_template_dialog)
        self.w(QAction, "actionSaveTemplate").triggered.connect(self.save_template_dialog)
        self.w(QAction, "actionQuit").triggered.connect(self.ui.close)
        self.w(QAction, "actionTest").triggered.connect(self.test_current)
        self.w(QAction, "actionFit").triggered.connect(self.canvas.fit)
        self.w(QAction, "actionChooseFolder").triggered.connect(self.choose_folder)
        self.w(QAction, "actionApply").triggered.connect(self.apply_tags)
        self.w(QPushButton, "chooseFolderButton").clicked.connect(self.choose_folder)
        self.w(QPushButton, "outputFolderButton").clicked.connect(self.choose_output)
        self.w(QPushButton, "addPageTypeButton").clicked.connect(self.add_page_type)
        self.w(QPushButton, "removePageTypeButton").clicked.connect(self.remove_page_type)
        self.w(QListWidget, "pageTypeList").currentRowChanged.connect(self._page_type_changed)
        self.w(QPushButton, "readButton").clicked.connect(self.read_selected)
        self.w(QPushButton, "useReadingButton").clicked.connect(self.use_reading)
        self.w(QPushButton, "keepAllButton").clicked.connect(lambda: self._set_all_conflicts("keep"))
        self.w(QPushButton, "overwriteAllButton").clicked.connect(lambda: self._set_all_conflicts("overwrite"))
        self.w(QListWidget, "filmstrip").currentRowChanged.connect(self._filmstrip_changed)
        self.w(QTableWidget, "batchTable").itemSelectionChanged.connect(self._batch_selection_changed)
        self.w(QComboBox, "pageTypeCombo").currentIndexChanged.connect(self._inspector_page_type_changed)
        self.w(QComboBox, "matchCombo").currentIndexChanged.connect(self._match_changed)
        self.w(QDoubleSpinBox, "padSpin").valueChanged.connect(self._pad_changed)
        self.w(QComboBox, "roleCombo").currentIndexChanged.connect(self._role_changed)
        self.w(QLineEdit, "tagEdit").editingFinished.connect(self._tag_edited)
        self.w(QComboBox, "scopeCombo").currentIndexChanged.connect(self._identifier_fields_changed)
        self.w(QComboBox, "opCombo").currentIndexChanged.connect(self._identifier_fields_changed)
        self.w(QPlainTextEdit, "expectedEdit").textChanged.connect(self._identifier_fields_changed)
        self.canvas.zoneDrawn.connect(self.add_drawn_box)
        self.canvas.zoneSelected.connect(self._zone_selected)
        self.canvas.zoneGeometryChanged.connect(self._zone_geometry)
        self.canvas.zoneDeleteRequested.connect(self.delete_selected_zone)
        app = QGuiApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    def log(self, message: str) -> None:
        self.w(QPlainTextEdit, "logView").appendPlainText(message)

    def shutdown(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.requestInterruption()
            self.worker.wait(4000)
        if self.doc is not None:
            self.doc.close()
            self.doc = None

    def current_page_type(self):
        row = self.w(QListWidget, "pageTypeList").currentRow()
        if row < 0 or row >= len(self.template.page_types):
            return None
        return self.template.page_types[row]

    def _refresh_page_types(self, select_id: str | None = None) -> None:
        listing = self.w(QListWidget, "pageTypeList")
        combo = self.w(QComboBox, "pageTypeCombo")
        current = self.current_page_type()
        target = select_id or (current.id if current else None)
        self._loading = True
        listing.clear()
        combo.clear()
        selected_row = 0
        for index, page_type in enumerate(self.template.page_types):
            listing.addItem(page_type.name)
            combo.addItem(page_type.name, page_type.id)
            if page_type.id == target:
                selected_row = index
        self._loading = False
        if self.template.page_types:
            listing.setCurrentRow(selected_row)
        self._sync_template_label()
        self._show_zones()

    def _sync_template_label(self) -> None:
        location = self.template_path.name if self.template_path else "unsaved"
        self.w(QLabel, "templateLabel").setText(f"Template: {self.template.name} ({location})")

    def _page_type_changed(self, _row: int) -> None:
        if self._loading:
            return
        page_type = self.current_page_type()
        self._loading = True
        if page_type is not None:
            combo = self.w(QComboBox, "pageTypeCombo")
            index = combo.findData(page_type.id)
            if index >= 0:
                combo.setCurrentIndex(index)
            self.w(QComboBox, "matchCombo").setCurrentIndex(0 if page_type.match != "any" else 1)
        self._loading = False
        self._show_zones()

    def _match_changed(self) -> None:
        if self._loading:
            return
        page_type = self.current_page_type()
        if page_type is None:
            return
        page_type.match = "any" if self.w(QComboBox, "matchCombo").currentIndex() == 1 else "all"

    def _pad_changed(self, value: float) -> None:
        if self._loading:
            return
        self.template.pad = float(value) / 100.0

    def add_page_type(self) -> None:
        name, accepted = QInputDialog.getText(self.ui, "Page type", "Name of this page type")
        if not accepted or not name.strip():
            return
        page_type = self.template.add_page_type(name)
        self._refresh_page_types(page_type.id)

    def remove_page_type(self) -> None:
        page_type = self.current_page_type()
        if page_type is None or len(self.template.page_types) <= 1:
            return
        self.template.page_types = [item for item in self.template.page_types if item.id != page_type.id]
        self.selected_box = ""
        self._refresh_page_types()

    def open_pdf_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self.ui, "Open PDF", str(self.batch_dir or Path.home()), "PDF (*.pdf)")
        if path:
            self.open_pdf(Path(path))

    def open_pdf(self, path: Path) -> None:
        try:
            document = pymupdf.open(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self.ui, "Open PDF", str(exc))
            return
        if document.needs_pass:
            document.close()
            QMessageBox.warning(self.ui, "Open PDF", "This PDF is encrypted.")
            return
        if self.doc is not None:
            self.doc.close()
        self.doc = document
        self.doc_path = path
        self.page_index = 0
        self.page_reports = []
        self._show_page()
        self._build_filmstrip()
        self.log(f"Opened {path} ({document.page_count} pages)")
        self.ui.statusBar().showMessage(str(path))

    def _show_page(self) -> None:
        if self.doc is None:
            return
        image = png_image(render_png(self.doc[self.page_index], 1.5))
        self.canvas.set_page_image(image)
        self._show_zones()
        self.ui.statusBar().showMessage(
            f"Page {self.page_index + 1} of {self.doc.page_count}    {self.doc_path}"
        )

    def _build_filmstrip(self) -> None:
        strip = self.w(QListWidget, "filmstrip")
        strip.blockSignals(True)
        strip.clear()
        if self.doc is None:
            strip.blockSignals(False)
            return
        reports = {report.index: report for report in self.page_reports}
        for index in range(self.doc.page_count):
            icon = QIcon(QPixmap.fromImage(png_image(render_png(self.doc[index], 0.16))))
            item = QListWidgetItem(icon, str(index + 1))
            item.setData(Qt.ItemDataRole.UserRole, index)
            report = reports.get(index)
            if report is not None:
                item.setBackground(QColor(PAGE_COLORS.get(report.status, "#3a414b")))
                item.setToolTip(report.note or report.status)
            strip.addItem(item)
        strip.setCurrentRow(self.page_index)
        strip.blockSignals(False)
        strip.setViewMode(strip.ViewMode.IconMode)
        strip.setResizeMode(strip.ResizeMode.Fixed)
        strip.setUniformItemSizes(True)
        strip.setMovement(strip.Movement.Static)

    def _filmstrip_changed(self, row: int) -> None:
        if row < 0 or self.doc is None or row == self.page_index:
            return
        # Finish the thumbnail click before replacing the page. Doing it inside
        # the click deletes scene items and relayouts the filmstrip mid-event.
        self.page_index = row
        if self._page_change_pending:
            return
        self._page_change_pending = True
        QTimer.singleShot(0, self._show_queued_page)

    def _show_queued_page(self) -> None:
        self._page_change_pending = False
        self._show_page()

    def _show_zones(self) -> None:
        page_type = self.current_page_type()
        boxes = []
        if page_type is not None:
            for identifier in page_type.identifiers:
                if identifier.rect is None:
                    continue
                boxes.append((identifier.id, "identifier", identifier.rect, identifier.value or "Identifier"))
            for zone in page_type.zones:
                boxes.append((zone.id, "value", zone.rect, zone.tag or "Value"))
        self._refreshing = True
        self.canvas.sync_zones(boxes, self.selected_box)
        self._refreshing = False

    def add_drawn_box(self, x: float, y: float, w: float, h: float) -> None:
        page_type = self.current_page_type()
        if page_type is None:
            return
        rect = RectN(x, y, w, h).clamped()
        if self.w(QComboBox, "roleCombo").currentIndex() == 1:
            box = Identifier(
                id=new_id(),
                scope="page" if self.w(QComboBox, "scopeCombo").currentIndex() == 1 else "region",
                op=self.w(QComboBox, "opCombo").currentText(),
                value=self.w(QPlainTextEdit, "expectedEdit").toPlainText().strip(),
                rect=rect,
            )
            page_type.identifiers.append(box)
        else:
            tag = sanitize_tag(self.w(QLineEdit, "tagEdit").text()) or f"Field{len(page_type.zones) + 1}"
            box = Zone(id=new_id(), tag=tag, rect=rect)
            page_type.zones.append(box)
            self.w(QLineEdit, "tagEdit").setText(tag)
        self.selected_box = box.id
        self._show_zones()
        self._load_inspector(box.id)

    def _zone_selected(self, box_id: str) -> None:
        if self._refreshing:
            return
        self.selected_box = box_id
        self._load_inspector(box_id)

    def _load_inspector(self, box_id: str) -> None:
        found = self.template.find_box(box_id) if box_id else None
        self._loading = True
        if found is None:
            self._set_inspector_enabled(False)
            self.w(QPlainTextEdit, "readEdit").setPlainText("")
            self._loading = False
            return
        page_type, _kind, box = found
        self._set_inspector_enabled(True)
        combo = self.w(QComboBox, "pageTypeCombo")
        index = combo.findData(page_type.id)
        if index >= 0:
            combo.setCurrentIndex(index)
        if isinstance(box, Zone):
            self.w(QComboBox, "roleCombo").setCurrentIndex(0)
            self.w(QLineEdit, "tagEdit").setText(box.tag)
            self.w(QPlainTextEdit, "expectedEdit").setPlainText("")
        else:
            self.w(QComboBox, "roleCombo").setCurrentIndex(1)
            self.w(QLineEdit, "tagEdit").setText("")
            self.w(QComboBox, "scopeCombo").setCurrentIndex(1 if box.scope == "page" else 0)
            op_index = self.w(QComboBox, "opCombo").findText(box.op)
            self.w(QComboBox, "opCombo").setCurrentIndex(max(op_index, 0))
            self.w(QPlainTextEdit, "expectedEdit").setPlainText(box.value)
        self._apply_role_visibility()
        self._loading = False

    def _set_inspector_enabled(self, enabled: bool) -> None:
        for name in ("roleCombo", "tagEdit", "scopeCombo", "opCombo", "expectedEdit", "readButton", "useReadingButton"):
            self.w(QWidget, name).setEnabled(enabled)
        self._apply_role_visibility()

    def _apply_role_visibility(self) -> None:
        is_identifier = self.w(QComboBox, "roleCombo").currentIndex() == 1 and self.w(QComboBox, "roleCombo").isEnabled()
        for name in ("scopeCombo", "opCombo", "expectedEdit", "useReadingButton"):
            self.w(QWidget, name).setEnabled(is_identifier)
        self.w(QLineEdit, "tagEdit").setEnabled(self.w(QComboBox, "roleCombo").isEnabled() and not is_identifier)

    def _inspector_page_type_changed(self) -> None:
        if self._loading:
            return
        found = self.template.find_box(self.selected_box) if self.selected_box else None
        target_id = self.w(QComboBox, "pageTypeCombo").currentData()
        if found is None:
            row = next((i for i, item in enumerate(self.template.page_types) if item.id == target_id), -1)
            if row >= 0:
                self.w(QListWidget, "pageTypeList").setCurrentRow(row)
            return
        page_type, _kind, box = found
        if page_type.id == target_id:
            return
        if not isinstance(target_id, str):
            return
        target = self.template.page_type(target_id)
        if target is None:
            return
        self.template.remove_box(box.id)
        if isinstance(box, Identifier):
            target.identifiers.append(box)
        else:
            target.zones.append(box)
        self._refresh_page_types(target.id)
        self._load_inspector(box.id)

    def _role_changed(self) -> None:
        if self._loading or not self.selected_box:
            self._apply_role_visibility()
            return
        found = self.template.find_box(self.selected_box)
        if found is None:
            return
        page_type, kind, box = found
        want = "identifier" if self.w(QComboBox, "roleCombo").currentIndex() == 1 else "value"
        if want == kind:
            self._apply_role_visibility()
            return
        rect = box.rect
        if want == "value" and rect is None:
            self._load_inspector(box.id)
            return
        self.template.remove_box(box.id)
        if want == "identifier":
            created = Identifier(id=box.id, rect=rect, value="")
            page_type.identifiers.append(created)
        elif rect is not None:
            created = Zone(id=box.id, tag=sanitize_tag(self.w(QLineEdit, "tagEdit").text()) or "Field", rect=rect)
            page_type.zones.append(created)
        self._show_zones()
        self._load_inspector(box.id)

    def _tag_edited(self) -> None:
        if self._loading:
            return
        found = self.template.find_box(self.selected_box) if self.selected_box else None
        if found is None:
            return
        box = found[2]
        if not isinstance(box, Zone):
            return
        tag = sanitize_tag(self.w(QLineEdit, "tagEdit").text())
        box.tag = tag
        self._loading = True
        self.w(QLineEdit, "tagEdit").setText(tag)
        self._loading = False
        self._show_zones()

    def _identifier_fields_changed(self) -> None:
        if self._loading:
            return
        found = self.template.find_box(self.selected_box) if self.selected_box else None
        if found is None:
            return
        identifier = found[2]
        if not isinstance(identifier, Identifier):
            return
        identifier.scope = "page" if self.w(QComboBox, "scopeCombo").currentIndex() == 1 else "region"
        identifier.op = self.w(QComboBox, "opCombo").currentText()
        identifier.value = self.w(QPlainTextEdit, "expectedEdit").toPlainText().strip()

    def _zone_geometry(self, box_id: str, x: float, y: float, w: float, h: float) -> None:
        found = self.template.find_box(box_id)
        if found is None:
            return
        found[2].rect = RectN(x, y, w, h).clamped()

    def delete_selected_zone(self) -> None:
        if not self.selected_box:
            return
        self.template.remove_box(self.selected_box)
        self.selected_box = ""
        self._show_zones()
        self._load_inspector("")

    def read_selected(self) -> None:
        found = self.template.find_box(self.selected_box) if self.selected_box else None
        if found is None or self.doc is None:
            return
        _page_type, _kind, box = found
        if isinstance(box, Identifier) and box.scope == "page":
            rect = None
        else:
            rect = box.rect
        pad = 0.0 if isinstance(box, Identifier) else self.template.pad
        text, source = read_region(self.doc[self.page_index], rect, pad=pad, allow_ocr=True)
        self.w(QPlainTextEdit, "readEdit").setPlainText(text)
        extra = f" ({source})"
        if source == "empty" and ocr_error():
            extra = f" (OCR unavailable: {ocr_error()})"
        self.log(f"Read {box.id}{extra}: {text}")

    def use_reading(self) -> None:
        found = self.template.find_box(self.selected_box) if self.selected_box else None
        if found is None or found[1] != "identifier":
            return
        text = self.w(QPlainTextEdit, "readEdit").toPlainText().strip()
        self.w(QPlainTextEdit, "expectedEdit").setPlainText(text)

    def open_template_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self.ui, "Open template", str(self.template_path or Path.home()), "ZoneTag template (*.zonetag.json *.json)"
        )
        if path:
            self.load_template(Path(path))

    def load_template(self, path: Path) -> None:
        try:
            self.template = Template.load(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self.ui, "Open template", str(exc))
            return
        if not self.template.page_types:
            self.template.add_page_type("Page A")
        self.template_path = path
        self.selected_box = ""
        self._loading = True
        self.w(QDoubleSpinBox, "padSpin").setValue(self.template.pad * 100.0)
        self._loading = False
        self._refresh_page_types()
        self.log(f"Loaded template {path}")

    def save_template_dialog(self) -> None:
        suggested = str(self.template_path or (Path.home() / f"{self.template.name}.zonetag.json"))
        path, _ = QFileDialog.getSaveFileName(self.ui, "Save template", suggested, "ZoneTag template (*.zonetag.json)")
        if not path:
            return
        if not path.endswith(".zonetag.json") and not path.endswith(".json"):
            path += ".zonetag.json"
        self.template.name = Path(path).stem
        try:
            self.template.save(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self.ui, "Save template", str(exc))
            return
        self.template_path = Path(path)
        self._sync_template_label()
        self.log(f"Saved template {path}")

    def test_current(self) -> None:
        if self.doc_path is None:
            QMessageBox.information(self.ui, "Test", "Open a sample PDF first.")
            return
        plan = analyze(self.doc_path, self.template)
        self.page_reports = plan.pages
        self._build_filmstrip()
        self._fill_tag_table([plan])
        self._fill_conflicts([plan])
        self.log(f"Test {Path(plan.path).name}: {plan.status}. {plan.message}")
        for page in plan.pages:
            if page.status != "matched":
                names = ", ".join(page.type_names) or "none"
                self.log(f"  page {page.index + 1}: {page.status} ({names}) {page.note}")
        self.w(QTabWidget, "resultsTabs").setCurrentIndex(1 if plan.tags else 0)

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self.ui, "Folder of PDFs", str(self.batch_dir or Path.home()))
        if not folder:
            return
        self.batch_dir = Path(folder)
        if self.output_dir is None:
            self.output_dir = self.batch_dir / "ZoneTag-output"
            self.w(QLabel, "outputFolderLabel").setText(f"Output: {self.output_dir}")
        self.start_preview()

    def choose_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self.ui, "Output folder", str(self.output_dir or self.batch_dir or Path.home()))
        if not folder:
            return
        self.output_dir = Path(folder)
        self.w(QLabel, "outputFolderLabel").setText(f"Output: {self.output_dir}")

    def _pdfs_in_folder(self) -> list[Path]:
        if self.batch_dir is None:
            return []
        pattern = "**/*.pdf" if self.w(QCheckBox, "recurseBox").isChecked() else "*.pdf"
        found = [path for path in self.batch_dir.glob(pattern) if path.is_file()]
        if self.output_dir is not None:
            try:
                output = self.output_dir.resolve()
                found = [path for path in found if output not in path.resolve().parents and path.resolve() != output]
            except OSError:
                pass
        return sorted(found, key=lambda path: path.name.casefold())

    def start_preview(self) -> None:
        paths = self._pdfs_in_folder()
        self.plans = []
        self.w(QTableWidget, "batchTable").setRowCount(0)
        self.w(QTableWidget, "tagTable").setRowCount(0)
        self.w(QTableWidget, "conflictTable").setRowCount(0)
        if not paths:
            self.log(f"No PDF files in {self.batch_dir}")
            return
        if self.worker is not None and self.worker.isRunning():
            self.worker.requestInterruption()
            self.worker.wait(4000)
        snapshot = Template.from_dict(self.template.to_dict())
        self.log(f"Reading {len(paths)} PDF(s) from {self.batch_dir}")
        self._set_busy(True)
        self.worker = PreviewWorker(paths, snapshot)
        self.worker.file_done.connect(self._preview_file)
        self.worker.finished_ok.connect(self._preview_finished)
        self.worker.start()

    def _set_busy(self, busy: bool) -> None:
        enabled = not busy
        for name in ("actionApply", "actionChooseFolder", "actionTest"):
            self.w(QAction, name).setEnabled(enabled)
        self.w(QPushButton, "chooseFolderButton").setEnabled(enabled)

    def _preview_file(self, plan: FilePlan) -> None:
        self.plans.append(plan)
        self._append_batch_row(plan)
        self.log(f"{Path(plan.path).name}: {plan.status}. {plan.message}")

    def _preview_finished(self) -> None:
        self._set_busy(False)
        self._fill_tag_table(self.plans)
        self._fill_conflicts(self.plans)
        conflicts = sum(1 for plan in self.plans for tag in plan.tags if tag.status in {"conflict", "ambiguous"})
        self.log(f"Batch list ready. {len(self.plans)} file(s), {conflicts} tag(s) need a choice.")
        if conflicts:
            self.w(QTabWidget, "resultsTabs").setCurrentIndex(2)

    def _append_batch_row(self, plan: FilePlan) -> None:
        table = self.w(QTableWidget, "batchTable")
        row = table.rowCount()
        table.insertRow(row)
        values = [Path(plan.path).name, plan.status, plan.message]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setData(Qt.ItemDataRole.UserRole, plan.path)
            item.setBackground(QColor(STATUS_COLORS.get(plan.status, "#3a414b")))
            table.setItem(row, column, item)

    def _batch_selection_changed(self) -> None:
        table = self.w(QTableWidget, "batchTable")
        row = table.currentRow()
        if row < 0:
            return
        item = table.item(row, 0)
        if item is None:
            return
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        plan = next((entry for entry in self.plans if Path(entry.path) == path), None)
        reports = plan.pages if plan is not None else None
        if self.doc_path != path:
            self.open_pdf(path)
        if reports is not None:
            self.page_reports = reports
            self._build_filmstrip()

    def _fill_tag_table(self, plans: list[FilePlan]) -> None:
        table = self.w(QTableWidget, "tagTable")
        table.setRowCount(0)
        for plan in plans:
            for tag in plan.tags:
                row = table.rowCount()
                table.insertRow(row)
                values = [Path(plan.path).name, tag.tag, tag.new_value or tag.existing or "", tag.status]
                for column, value in enumerate(values):
                    table.setItem(row, column, QTableWidgetItem(value))

    def _fill_conflicts(self, plans: list[FilePlan]) -> None:
        table = self.w(QTableWidget, "conflictTable")
        table.setRowCount(0)
        for plan in plans:
            for tag in plan.tags:
                if tag.status not in {"conflict", "ambiguous"}:
                    continue
                row = table.rowCount()
                table.insertRow(row)
                values = [
                    Path(plan.path).name,
                    tag.tag,
                    tag.existing or "",
                    tag.new_value,
                    "",
                    tag.note,
                ]
                for column, value in enumerate(values):
                    if column == 4:
                        continue
                    item = QTableWidgetItem(value)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    table.setItem(row, column, item)
                combo = QComboBox()
                for label, decision, write_value in tag.options:
                    combo.addItem(label, (decision, write_value))
                combo.setProperty("plan_path", plan.path)
                combo.setProperty("tag_name", tag.tag)
                combo.currentIndexChanged.connect(lambda _index, box=combo: self._conflict_choice(box))
                table.setCellWidget(row, 4, combo)

    def _conflict_choice(self, combo: QComboBox) -> None:
        decision, write_value = combo.currentData()
        plan = next((entry for entry in self.plans if entry.path == combo.property("plan_path")), None)
        if plan is None:
            return
        tag = next((item for item in plan.tags if item.tag == combo.property("tag_name")), None)
        if tag is None:
            return
        tag.decision = decision
        tag.write_value = write_value or ""

    def _set_all_conflicts(self, choice: str) -> None:
        table = self.w(QTableWidget, "conflictTable")
        for row in range(table.rowCount()):
            combo = table.cellWidget(row, 4)
            if not isinstance(combo, QComboBox) or combo.count() == 0:
                continue
            combo.setCurrentIndex(0 if choice == "keep" else combo.count() - 1)

    def apply_tags(self) -> None:
        if not self.plans:
            QMessageBox.information(self.ui, "Apply", "Choose a folder and let the batch list fill first.")
            return
        if self.output_dir is None:
            self.choose_output()
            if self.output_dir is None:
                return
        jobs = []
        for plan in self.plans:
            chosen = tags_to_write(plan)
            if chosen:
                jobs.append((plan, chosen))
        if not jobs:
            QMessageBox.information(
                self.ui,
                "Apply",
                "Nothing to write. New tags are written automatically. Existing different values stay until you choose Overwrite.",
            )
            return
        answer = QMessageBox.question(
            self.ui,
            "Apply tags",
            f"Write {len(jobs)} PDF copy(s) to\n{self.output_dir}?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        written = 0
        for plan, chosen in jobs:
            try:
                dest = unique_dest(self.output_dir, Path(plan.path).name)
                write_tags(plan.path, dest, chosen)
            except Exception as exc:  # noqa: BLE001
                self.log(f"Failed {Path(plan.path).name}: {exc}")
                continue
            written += 1
            self.log(f"Wrote {dest.name}: " + ", ".join(f"{key}={value}" for key, value in chosen.items()))
        self.log(f"Finished. {written} file(s) written.")
        QMessageBox.information(self.ui, "Apply", f"Wrote {written} file(s) to {self.output_dir}.")
