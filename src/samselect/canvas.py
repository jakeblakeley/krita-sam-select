"""Find Krita's canvas widget and map between widget and image pixels."""

from __future__ import annotations

from PyQt5.QtCore import QPointF
from PyQt5.QtGui import QTransform
from PyQt5.QtWidgets import QAbstractScrollArea, QMdiArea, QWidget

CANVAS_CLASSES = ("KisOpenGLCanvas2", "KisQPainterCanvas")


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
