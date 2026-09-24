"""Cursors in the style of Krita's Freehand Selection tool, marked with a sparkle.

Krita's selection cursors are compiled into the selection-tools plugin as Qt
resources, e.g. ``:/tool_outline_selection_cursor_add.png`` (an arrow with a
dotted lasso badge showing the mode, hotspot 5,5 in kis_tool_select_outline).
We reuse them so the mode feedback is identical to the freehand lasso, and add
a small sparkle to tell SAM Select apart. If the resources are missing, draw
an equivalent.
"""

from __future__ import annotations

from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import QColor, QCursor, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

from . import modes

HOTSPOT = (5, 5)
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
    """Arrow + dotted lasso badge, like tool_outline_selection_cursor*.png."""
    pm = QPixmap(32, 32)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    arrow = QPolygonF([QPointF(5, 5), QPointF(5, 16), QPointF(8, 13), QPointF(10, 17), QPointF(12, 16), QPointF(10, 12), QPointF(14, 12)])
    p.setPen(QPen(QColor(255, 255, 255), 1.5))
    p.setBrush(QColor(0, 0, 0))
    p.drawPolygon(arrow)
    p.setPen(QPen(QColor(0, 0, 0), 1, Qt.DotLine))
    p.setBrush(QColor(255, 255, 255, 170))
    p.drawEllipse(QPointF(23, 23), 7, 5)
    p.setPen(QPen(QColor(0, 0, 0), 1.6))
    c = QPointF(23, 23)
    if mode in (modes.ADD, modes.SUBTRACT):
        p.drawLine(c + QPointF(-3, 0), c + QPointF(3, 0))
    if mode == modes.ADD:
        p.drawLine(c + QPointF(0, -3), c + QPointF(0, 3))
    if mode == modes.INTERSECT:
        p.drawLine(c + QPointF(-2.5, -2.5), c + QPointF(2.5, 2.5))
        p.drawLine(c + QPointF(-2.5, 2.5), c + QPointF(2.5, -2.5))
    if mode == modes.SYMMETRIC_DIFFERENCE:
        p.drawPolygon(QPolygonF([c + QPointF(0, -3), c + QPointF(3, 2.5), c + QPointF(-3, 2.5)]))
    p.end()
    return pm


def cursor(mode: str) -> QCursor:
    if mode in _cache:
        return _cache[mode]
    pm = QPixmap(f":/tool_outline_selection_cursor{_SUFFIX.get(mode, '')}.png")
    pm = _fallback(mode) if pm.isNull() else pm.copy()
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    _sparkle(p, 19.0, 5.5, 4.0)
    p.end()
    _cache[mode] = QCursor(pm, *HOTSPOT)
    return _cache[mode]
