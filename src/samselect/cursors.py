"""Cursors in the style of Krita's selection tools, marked with a sparkle.

Krita's selection cursors (crosshair at 6,6 plus a dotted badge showing the
mode) are compiled into the selection-tools plugin as Qt resources, e.g.
``:/tool_rectangular_selection_cursor_add.png``. We reuse them so the mode
feedback is pixel-identical to the built-in tools, and add a small sparkle to
tell SAM Select apart. If the resources are missing, draw an equivalent.
"""

from __future__ import annotations

from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import QColor, QCursor, QPainter, QPainterPath, QPen, QPixmap

from . import modes

_SUFFIX = {
    modes.REPLACE: "",
    modes.ADD: "_add",
    modes.SUBTRACT: "_sub",
    modes.INTERSECT: "_inter",
    modes.SYMMETRIC_DIFFERENCE: "_symdiff",
}
_cache: dict[str, QCursor] = {}


def _sparkle(p: QPainter, cx: float, cy: float, r: float) -> None:
    path = QPainterPath()
    path.moveTo(cx, cy - r)
    for dx, dy in ((0.28, -0.28), (1, 0), (0.28, 0.28), (0, 1), (-0.28, 0.28), (-1, 0), (-0.28, -0.28), (0, -1)):
        path.lineTo(cx + dx * r, cy + dy * r)
    p.setPen(QPen(QColor(0, 0, 0), 1.0))
    p.setBrush(QColor(255, 255, 255))
    p.drawPath(path)


def _fallback(mode: str) -> QPixmap:
    pm = QPixmap(32, 32)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    for color, width in ((QColor(0, 0, 0), 3), (QColor(255, 255, 255), 1)):
        p.setPen(QPen(color, width))
        p.drawLine(6, 0, 6, 12)
        p.drawLine(0, 6, 12, 6)
    if mode != modes.REPLACE:
        p.setPen(QPen(QColor(0, 0, 0), 1, Qt.DotLine))
        p.setBrush(QColor(255, 255, 255, 200))
        p.drawRect(16, 16, 14, 14)
        p.setPen(QPen(QColor(0, 0, 0), 2))
        c = QPointF(23, 23)
        if mode in (modes.ADD, modes.SUBTRACT):
            p.drawLine(c + QPointF(-4, 0), c + QPointF(4, 0))
        if mode == modes.ADD:
            p.drawLine(c + QPointF(0, -4), c + QPointF(0, 4))
        if mode == modes.INTERSECT:
            p.drawLine(c + QPointF(-3, -3), c + QPointF(3, 3))
            p.drawLine(c + QPointF(-3, 3), c + QPointF(3, -3))
        if mode == modes.SYMMETRIC_DIFFERENCE:
            p.drawPolygon(c + QPointF(0, -4), c + QPointF(4, 3), c + QPointF(-4, 3))
    p.end()
    return pm


def cursor(mode: str) -> QCursor:
    if mode in _cache:
        return _cache[mode]
    pm = QPixmap(f":/tool_rectangular_selection_cursor{_SUFFIX.get(mode, '')}.png")
    if pm.isNull():
        pm = _fallback(mode)
    else:
        pm = pm.copy()
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    _sparkle(p, 17.5, 4.5, 4.0)
    p.end()
    _cache[mode] = QCursor(pm, 6, 6)
    return _cache[mode]
