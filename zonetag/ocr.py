"""Local OCR for boxes that have no PDF text layer."""

from __future__ import annotations

import numpy as np

_engine = None
_error: str | None = None


def ocr_error() -> str | None:
    return _error


def recognize(image) -> str:
    """Read a RGB or BGR ndarray. Returns an empty string when OCR cannot run."""
    global _engine, _error
    if _error:
        return ""
    try:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR

            _engine = RapidOCR()
        result, _elapsed = _engine(image)
    except Exception as exc:  # noqa: BLE001 - surface any OCR runtime failure once
        _error = str(exc)
        return ""
    if not result:
        return ""
    parts: list[str] = []
    for row in result:
        if isinstance(row, (list, tuple)) and len(row) >= 2 and row[1]:
            parts.append(str(row[1]))
    return " ".join(parts)


def pixmap_to_bgr(pix) -> np.ndarray:
    channels = pix.n
    array = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, channels)
    if channels == 4:
        array = array[:, :, :3]
    return np.ascontiguousarray(array[:, :, ::-1])
