"""Page view: rendered PDF page plus movable zone rectangles."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import shiboken6  # type: ignore[import-not-found]
from PySide6.QtCore import QObject, QPoint, QRectF, Qt, Signal  # type: ignore[import-not-found]
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut  # type: ignore[import-not-found]
from PySide6.QtWidgets import (  # type: ignore[import-not-found]
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

from zonetag.model import RectN


IDENTIFIER_COLOR = QColor("#3d8bfd")
VALUE_COLOR = QColor("#3dd68c")
SELECTED_COLOR = QColor("#ffd166")


class HandleItem(QGraphicsRectItem):
    def __init__(self, corner: str, parent: ZoneItem):
        super().__init__(parent)
        self.corner = corner
        self.setBrush(SELECTED_COLOR)
        self.setPen(QPen(Qt.PenStyle.NoPen))
        self.setZValue(2)
        cursors = {
            "nw": Qt.CursorShape.SizeFDiagCursor,
            "se": Qt.CursorShape.SizeFDiagCursor,
            "ne": Qt.CursorShape.SizeBDiagCursor,
            "sw": Qt.CursorShape.SizeBDiagCursor,
        }
        self.setCursor(cursors[corner])

    def _zone(self) -> ZoneItem | None:
        parent = self.parentItem()
        return parent if isinstance(parent, ZoneItem) else None

    def mousePressEvent(self, event):
        zone = self._zone()
        if zone is not None:
            zone.begin_resize()
        event.accept()

    def mouseMoveEvent(self, event):
        zone = self._zone()
        if zone is not None:
            zone.resize_to(self.corner, event.scenePos())
        event.accept()

    def mouseReleaseEvent(self, event):
        zone = self._zone()
        if zone is not None:
            zone.finish_edit()
        event.accept()


class ZoneItem(QGraphicsRectItem):
    def __init__(self, zone_id: str, kind: str, rect: QRectF):
        super().__init__(rect)
        self.zone_id = zone_id
        self.kind = kind
        self.on_edit: Callable[[ZoneItem], None] | None = None
        flags = (
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setFlags(flags)
        self._label: QGraphicsSimpleTextItem | None = QGraphicsSimpleTextItem(self)
        self._label.setBrush(QColor("#f4f7fb"))  # type: ignore[import-not-found]
        self._handles = {corner: HandleItem(corner, self) for corner in ("nw", "ne", "sw", "se")}
        self.update_look()
        self.sync_handles()

    def set_caption(self, text: str) -> None:
        if self._label is None:
            return
        self._label.setText(text[:42])
        self._label.setPos(self.rect().left() + 4, self.rect().top() + 2)

    def detach_children(self) -> None:
        """Take handles out of the scene before this box is destroyed."""
        scene = self.scene()
        # Qt detaches an item when the parent is null. The stub requires a QGraphicsItem.
        for handle in list(self._handles.values()):
            if scene is not None and handle.scene() is scene:
                scene.removeItem(handle)
            handle.setParentItem(cast(QGraphicsItem, None))
        self._handles.clear()
        if self._label is not None:
            if scene is not None and self._label.scene() is scene:
                scene.removeItem(self._label)
            self._label.setParentItem(cast(QGraphicsItem, None))
            self._label = None

    def sync_handles(self) -> None:
        rect = self.rect()
        size = 8.0
        points = {
            "nw": (rect.left(), rect.top()),
            "ne": (rect.right(), rect.top()),
            "sw": (rect.left(), rect.bottom()),
            "se": (rect.right(), rect.bottom()),
        }
        for corner, (x, y) in points.items():
            self._handles[corner].setRect(QRectF(x - size / 2, y - size / 2, size, size))
        if self._label is not None:
            self._label.setPos(rect.left() + 4, rect.top() + 2)

    def update_look(self) -> None:
        color = IDENTIFIER_COLOR if self.kind == "identifier" else VALUE_COLOR
        if self.isSelected():
            color = SELECTED_COLOR
        self.setPen(QPen(color, 2))
        fill = QColor(color)
        fill.setAlpha(48)
        self.setBrush(fill)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.update_look()
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.sync_handles()
        return super().itemChange(change, value)

    def begin_resize(self) -> None:
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)

    def resize_to(self, corner: str, scene_pos) -> None:
        local = self.mapFromScene(scene_pos)
        rect = self.rect()
        left, top, right, bottom = rect.left(), rect.top(), rect.right(), rect.bottom()
        if "n" in corner:
            top = local.y()
        if "s" in corner:
            bottom = local.y()
        if "w" in corner:
            left = local.x()
        if "e" in corner:
            right = local.x()
        if right - left < 8:
            right = left + 8
        if bottom - top < 8:
            bottom = top + 8
        self.setRect(QRectF(left, top, right - left, bottom - top).normalized())
        self.sync_handles()

    def finish_edit(self) -> None:
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        if self.on_edit is not None:
            self.on_edit(self)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self.finish_edit()


class PageCanvas(QObject):
    zoneDrawn = Signal(float, float, float, float)
    zoneSelected = Signal(str)
    zoneGeometryChanged = Signal(str, float, float, float, float)
    zoneDeleteRequested = Signal()

    def __init__(self, view: QGraphicsView):
        super().__init__(view)
        self.view = view
        self.scene = QGraphicsScene(self)
        self.view.setScene(self.scene)
        self.view.setRenderHints(
            self.view.renderHints()
            | QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.view.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.view.setBackgroundBrush(QColor("#1b1f24"))
        self._pix: QGraphicsPixmapItem | None = None
        self._origin: QPoint | None = None
        self._band: QGraphicsRectItem | None = None
        self.view.viewport().installEventFilter(self)
        self.scene.selectionChanged.connect(self._emit_selection)
        self._delete_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.view)
        self._delete_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._delete_shortcut.activated.connect(self.zoneDeleteRequested.emit)
        self._backspace_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Backspace), self.view)
        self._backspace_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._backspace_shortcut.activated.connect(self.zoneDeleteRequested.emit)

    def set_page_image(self, image: QImage) -> None:
        if image.isNull():
            return
        # Swap the pixmap in place. Rebuilding the scene here deletes the tag
        # boxes while a thumbnail click is still on the stack.
        pixmap = QPixmap.fromImage(image)
        if self._pix is None or not shiboken6.isValid(self._pix):
            self._pix = self.scene.addPixmap(pixmap)
            self._pix.setZValue(0)  # type: ignore[import-not-found]
        else:
            self._pix.setPixmap(pixmap)
        self._origin = None
        self._remove_band()
        self.scene.setSceneRect(self._pix.boundingRect())  # type: ignore[import-not-found]
        self.fit()

    def fit(self) -> None:
        if self._pix is not None:
            self.view.fitInView(self._pix, Qt.AspectRatioMode.KeepAspectRatio)

    def clear_zones(self) -> None:
        self.scene.blockSignals(True)
        zones = [item for item in self.scene.items() if isinstance(item, ZoneItem) and shiboken6.isValid(item)]
        for item in zones:
            item.detach_children()
            if item.scene() is self.scene:
                self.scene.removeItem(item)
        self.scene.blockSignals(False)

    def sync_zones(self, boxes: list[tuple[str, str, RectN, str]], selected_id: str) -> None:
        """Show `boxes` as (id, kind, rect, caption). Reuse boxes that are already on the page."""
        if self._pix is None:
            return
        existing: dict[str, ZoneItem] = {}
        for item in self.scene.items():
            if isinstance(item, ZoneItem) and shiboken6.isValid(item):
                existing[item.zone_id] = item
        wanted = {zone_id for zone_id, _kind, _rect, _caption in boxes}
        self.scene.blockSignals(True)
        for zone_id, item in existing.items():
            if zone_id not in wanted:
                item.detach_children()
                if item.scene() is self.scene:
                    self.scene.removeItem(item)
        for zone_id, kind, rect, caption in boxes:
            item = existing.get(zone_id)
            if item is None or not shiboken6.isValid(item) or item.scene() is not self.scene:
                self.scene.blockSignals(False)
                self.add_zone(zone_id, kind, rect, caption)
                self.scene.blockSignals(True)
                continue
            self._place(item, rect)
            item.kind = kind
            item.set_caption(caption)
            item.update_look()
        self.scene.blockSignals(False)
        if selected_id:
            self.select_zone(selected_id)

    def _place(self, item: ZoneItem, rect: RectN) -> None:
        pix = self._pix
        if pix is None:
            return
        page = pix.boundingRect()
        item.setPos(0, 0)
        item.setRect(
            QRectF(
                page.left() + (rect.x * page.width()),
                page.top() + (rect.y * page.height()),
                max(rect.w * page.width(), 8),
                max(rect.h * page.height(), 8),
            )
        )
        item.sync_handles()

    def add_zone(self, zone_id: str, kind: str, rect: RectN, caption: str) -> None:
        if self._pix is None:
            return
        page = self._pix.boundingRect()
        box = QRectF(
            page.left() + (rect.x * page.width()),
            page.top() + (rect.y * page.height()),
            rect.w * page.width(),
            rect.h * page.height(),
        )
        item = ZoneItem(zone_id, kind, box)
        item.set_caption(caption)
        item.on_edit = self._zone_edited
        item.setZValue(1)
        self.scene.addItem(item)

    def select_zone(self, zone_id: str) -> None:
        self.scene.blockSignals(True)
        for item in self.scene.items():
            if isinstance(item, ZoneItem):
                item.setSelected(item.zone_id == zone_id)
                item.update_look()
        self.scene.blockSignals(False)

    def eventFilter(self, obj, event):
        if obj is not self.view.viewport() or self._pix is None:
            return False
        event_type = event.type()
        if event_type == event.Type.Wheel and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            self.view.scale(factor, factor)
            return True
        if event_type == event.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            if self._zone_at(event.position().toPoint()) is not None:
                return False
            self._origin = event.position().toPoint()
            self._remove_band()
            self._band = self.scene.addRect(
                QRectF(),
                QPen(QColor("#8ec5ff"), 1, Qt.PenStyle.DashLine),
            )
            self._band.setZValue(5)  # type: ignore[import-not-found]
            return True
        if event_type == event.Type.MouseMove and self._origin is not None:
            self._update_band(event.position().toPoint())
            return True
        if event_type == event.Type.MouseButtonRelease and self._origin is not None:
            self._finish_band(event.position().toPoint())
            return True
        return False

    def _zone_at(self, pos: QPoint) -> ZoneItem | None:
        item = self.view.itemAt(pos)
        while item is not None:
            if isinstance(item, ZoneItem):
                return item
            item = item.parentItem()
        return None

    def _remove_band(self) -> None:
        if self._band is not None:
            self.scene.removeItem(self._band)
            self._band = None

    def _update_band(self, pos: QPoint) -> None:
        if self._band is None or self._origin is None:
            return
        self._band.setRect(self._scene_rect(self._origin, pos))

    def _finish_band(self, pos: QPoint) -> None:
        origin = self._origin
        self._origin = None
        rect = self._scene_rect(origin, pos) if origin is not None else QRectF()
        self._remove_band()
        normalized = self._normalized(rect)
        if normalized is None or normalized.w < 0.005 or normalized.h < 0.005:
            self.scene.clearSelection()
            return
        self.zoneDrawn.emit(normalized.x, normalized.y, normalized.w, normalized.h)

    def _scene_rect(self, start: QPoint, end: QPoint) -> QRectF:
        return QRectF(self.view.mapToScene(start), self.view.mapToScene(end)).normalized()

    def _normalized(self, rect: QRectF) -> RectN | None:
        if self._pix is None:
            return None
        page = self._pix.sceneBoundingRect()
        clipped = rect.intersected(page)
        if clipped.isEmpty() or page.width() == 0 or page.height() == 0:
            return None
        return RectN(
            (clipped.left() - page.left()) / page.width(),
            (clipped.top() - page.top()) / page.height(),
            clipped.width() / page.width(),
            clipped.height() / page.height(),
        )

    def _zone_edited(self, item: ZoneItem) -> None:
        normalized = self._normalized(item.sceneBoundingRect())
        if normalized is None:
            return
        self.zoneGeometryChanged.emit(item.zone_id, normalized.x, normalized.y, normalized.w, normalized.h)

    def _emit_selection(self) -> None:
        selected = [item for item in self.scene.selectedItems() if isinstance(item, ZoneItem)]
        self.zoneSelected.emit(selected[0].zone_id if len(selected) == 1 else "")
