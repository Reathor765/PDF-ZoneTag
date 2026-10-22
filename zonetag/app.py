"""Start the ZoneTag window."""

from __future__ import annotations

import sys

from PySide6.QtGui import QIcon  # type: ignore[import-not-found]
from PySide6.QtWidgets import QApplication  # type: ignore[import-not-found]

from zonetag.window import ICON_PATH, MainController, apply_editor_palette


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("ZoneTag")
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    apply_editor_palette(app)
    controller = MainController()
    controller.ui.resize(1440, 900)
    controller.ui.show()
    return app.exec()
