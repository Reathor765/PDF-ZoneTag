"""Start the ZoneTag window."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from zonetag.window import MainController, apply_editor_palette


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("ZoneTag")
    apply_editor_palette(app)
    controller = MainController()
    controller.ui.resize(1440, 900)
    controller.ui.show()
    return app.exec()
