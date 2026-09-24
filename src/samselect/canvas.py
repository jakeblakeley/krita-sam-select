"""Find Krita's canvas widget and map between widget and image pixels."""

from __future__ import annotations

from PyQt5.QtCore import QPointF, QRect, Qt
from PyQt5.QtGui import QTransform
from PyQt5.QtWidgets import QAbstractButton, QAbstractScrollArea, QMdiArea, QWidget

CANVAS_CLASSES = ("KisOpenGLCanvas2", "KisQPainterCanvas")
# Krita's floating selection actions bar (KisSelectionActionsPanel): real child
# widgets of the canvas for input, painted by the canvas itself. Its classes
# don't declare Q_OBJECT, so at runtime they only report "QAbstractButton" /
# "QWidget"; recognise them by what Krita sets on them instead: the buttons
# carry the long-press property, the drag handle an open/closed-hand cursor.
LONG_PRESS_PROPERTY = "KRITA_LONG_PRESS"
HANDLE_CURSORS = (Qt.OpenHandCursor, Qt.ClosedHandCursor)
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


def _bar_buttons(canvas) -> list:
    return [
        c
        for c in canvas.children()
        if isinstance(c, QAbstractButton) and c.isVisible() and c.property(LONG_PRESS_PROPERTY)
    ]


def _is_handle(widget, button_size, near: QRect) -> bool:
    return (
        not isinstance(widget, QAbstractButton)
        and widget.isVisible()
        and widget.size() == button_size
        and widget.cursor().shape() in HANDLE_CURSORS
        and widget.geometry().adjusted(-1, -1, 1, 1).intersects(near)
    )


def actions_bar_rect(canvas) -> QRect | None:
    """Where Krita's selection actions bar is drawn on the canvas, or None if hidden."""
    if canvas is None:
        return None
    buttons = _bar_buttons(canvas)
    if not buttons:
        return None
    rect = QRect()
    for b in buttons:
        rect = rect.united(b.geometry())
    size = buttons[0].size()
    for child in canvas.children():
        if isinstance(child, QWidget) and _is_handle(child, size, rect):
            rect = rect.united(child.geometry())
    m = ACTIONS_BAR_MARGIN
    return rect.adjusted(-m, -m, m, m)


def is_actions_bar_widget(obj, canvas) -> bool:
    """True for the bar's buttons and drag handle (children of ``canvas``)."""
    if canvas is None or not isinstance(obj, QWidget) or obj.parentWidget() is not canvas:
        return False
    if isinstance(obj, QAbstractButton):
        return bool(obj.property(LONG_PRESS_PROPERTY))
    buttons = _bar_buttons(canvas)
    if not buttons:
        return False
    rect = QRect()
    for b in buttons:
        rect = rect.united(b.geometry())
    return _is_handle(obj, buttons[0].size(), rect)
