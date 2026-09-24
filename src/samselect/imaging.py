"""Grab the pixels SAM sees, as a 1008 x 1008 RGB buffer.

The model squashes every image to 1008 x 1008, so we do the resize here (in
C++ via Qt, with area-averaging) instead of shipping full-resolution pixels to
the backend. Normalised coordinates then map 1:1 between document and model.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QImage, QPainter

MODEL_SIZE = 1008
# Above this many pixels, ask Krita for a pre-shrunk thumbnail instead of the
# full projection (which would allocate W*H*4 bytes on the UI thread).
FULL_PROJECTION_LIMIT = 24_000_000
THUMB_OVERSAMPLE = 2  # thumbnail at 2x model size, then area-average down


@dataclass
class Capture:
    key: str
    rgb: bytes  # MODEL_SIZE * MODEL_SIZE * 3
    width: int  # document size the mask must be rendered at
    height: int


def _source_image(doc, reference: str) -> QImage:
    w, h = doc.width(), doc.height()
    if reference == "layer":
        node = doc.activeNode()
        if node is not None:
            data = node.thumbnail(min(w, MODEL_SIZE * THUMB_OVERSAMPLE), min(h, MODEL_SIZE * THUMB_OVERSAMPLE))
            if not data.isNull():
                return data
    if w * h <= FULL_PROJECTION_LIMIT:
        img = doc.projection(0, 0, w, h)
        if not img.isNull():
            return img
    side = MODEL_SIZE * THUMB_OVERSAMPLE
    return doc.thumbnail(min(w, side), min(h, side))


def _flatten(img: QImage) -> QImage:
    """Composite transparency over a background that contrasts with the content."""
    img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    scaled = img.scaled(MODEL_SIZE, MODEL_SIZE, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    out = QImage(MODEL_SIZE, MODEL_SIZE, QImage.Format_RGB32)
    avg = scaled.scaled(1, 1, Qt.IgnoreAspectRatio, Qt.SmoothTransformation).pixelColor(0, 0)
    if avg.alpha() < 255:
        luma = 0.299 * avg.red() + 0.587 * avg.green() + 0.114 * avg.blue()
        out.fill(QColor(40, 40, 40) if luma > 150 else QColor(225, 225, 225))
    painter = QPainter(out)
    painter.drawImage(0, 0, scaled)
    painter.end()
    return out.convertToFormat(QImage.Format_RGB888)


def capture(doc, reference: str = "image") -> Capture | None:
    if doc is None or doc.width() <= 0 or doc.height() <= 0:
        return None
    src = _source_image(doc, reference)
    if src.isNull():
        return None
    rgb_img = _flatten(src)
    ptr = rgb_img.constBits()
    ptr.setsize(rgb_img.sizeInBytes())
    stride = rgb_img.bytesPerLine()
    raw = bytes(ptr)
    if stride != MODEL_SIZE * 3:  # never true for 1008 px RGB888, but be safe
        raw = b"".join(raw[y * stride : y * stride + MODEL_SIZE * 3] for y in range(MODEL_SIZE))
    key = hashlib.blake2b(raw, digest_size=16).hexdigest()
    return Capture(key, raw, doc.width(), doc.height())
