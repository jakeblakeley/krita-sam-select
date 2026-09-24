"""Find Krita's canvas widget and map between widget and image pixels."""

from __future__ import annotations

from PyQt5.QtCore import QPointF, QRect
from PyQt5.QtGui import QTransform
from PyQt5.QtWidgets import QAbstractScrollArea, QMdiArea, QWidget

CANVAS_CLASSES = ("KisOpenGLCanvas2", "KisQPainterCanvas")
# Krita's floating selection actions bar: real child widgets of the canvas for
# input, but painted by the canvas itself (KisSelectionActionsPanel::draw).
ACTIONS_BAR_CLASSES = ("KisSelectionActionsPanelButton", "KisSelectionActionsPanelHandle")
# The panel paints a 4 px outline plus a 1 px contrast ring around its buttons.
ACTIONS_BAR_MARGIN = 6


def canvas_widget(qwindow) -> QWidget | None:
    """The canvas widget of the window's active view (OpenGL or QPainter)."""
    mdi = qwindow.findChild(QMdiArea) if qwindow is not None else None
    sub = mdi.activeSubWindow() if mdi is not None else None
    root = sub.widget() if sub is not None else None
    if root is None:
        return None
    for area in root.findChildren(QAbstractScrollArea):
        for child in area.viewport().findChildren(QWidget):
            if child.metaObject().className() in CANVAS_CLASSES:
                return child
    for child in root.findChildren(QWidget):
        if child.metaObject().className() in CANVAS_CLASSES:
            return child
    return None


def image_to_widget(view) -> QTransform:
    """Image pixels -> canvas-widget logical pixels, including zoom, rotation and mirroring.

    libkis exposes flake->canvas-widget and flake->image; compose
    image->flake (inverse of the latter) with flake->widget.
    """
    image_to_flake, ok = view.flakeToImageTransform().inverted()
    if not ok:
        return QTransform()
    return image_to_flake * view.flakeToCanvasTransform()  # Qt: A * B applies A, then B


def widget_to_image(view) -> QTransform:
    inv, ok = image_to_widget(view).inverted()
    return inv if ok else QTransform()


def to_image(view, pos) -> QPointF:
    return widget_to_image(view).map(QPointF(pos))


def actions_bar_rect(canvas) -> QRect | None:
    """Where Krita's selection actions bar is drawn on the canvas, or None if hidden."""
    rect = QRect()
    for child in canvas.children():
        if isinstance(child, QWidget) and child.isVisible() and child.metaObject().className() in ACTIONS_BAR_CLASSES:
            rect = rect.united(child.geometry())
    if rect.isNull():
        return None
    m = ACTIONS_BAR_MARGIN
    return rect.adjusted(-m, -m, m, m)


def is_actions_bar_widget(obj) -> bool:
    return isinstance(obj, QWidget) and obj.metaObject().className() in ACTIONS_BAR_CLASSES
