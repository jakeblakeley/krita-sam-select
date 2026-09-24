"""On-canvas widgets: the preview overlay and the floating text prompt.

Both are children of Krita's canvas widget so they follow it through
resizes, tab switches and detaching. The overlay never takes mouse input;
everything it draws is positioned through the view's image -> widget
transform, so it stays glued to the artwork under zoom, rotation and mirroring.
"""

from __future__ import annotations

import math

from PyQt5.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPen, QPolygonF, QTransform
from PyQt5.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QToolButton, QWidget

PLACEHOLDER = "type what to select"


class CanvasOverlay(QWidget):
    """Transparent layer for hover previews, the lasso path and a busy spinner."""

    def __init__(self, canvas: QWidget) -> None:
        super().__init__(canvas)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.NoFocus)
        self.image_to_widget = QTransform()
        self.preview: QImage | None = None  # colourised mask, in "preview space"
        self.preview_to_image = QTransform()  # preview pixels -> image pixels
        self.lasso: QPolygonF | None = None  # freehand path, image coordinates
        self.busy = False
        self.busy_pos = QPointF()
        self._spin = 0
        self._spinner = QTimer(self)
        self._spinner.setInterval(40)
        self._spinner.timeout.connect(self._tick)
        self.color = QColor(255, 60, 110)
        canvas.installEventFilter(self)
        self.setGeometry(canvas.rect())
        self.show()
        self.raise_()

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Resize:
            self.setGeometry(obj.rect())
        return False

    def detach(self) -> None:
        parent = self.parentWidget()
        if parent is not None:
            parent.removeEventFilter(self)
        self._spinner.stop()
        self.hide()
        self.deleteLater()

    # ------------------------------------------------------------ content

    def set_preview(self, mask: bytes | None, x: int = 0, y: int = 0, w: int = 0, h: int = 0, scale: float = 1.0) -> None:
        """Show an 8-bit mask (preview space, ``scale`` preview px per image px)."""
        if mask is None or w <= 0 or h <= 0:
            if self.preview is not None:
                self.preview = None
                self.update()
            return
        alpha = QImage(mask, w, h, w, QImage.Format_Alpha8).copy()
        tinted = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        c = QColor(self.color)
        c.setAlpha(110)
        tinted.fill(c)
        p = QPainter(tinted)
        p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        p.drawImage(0, 0, alpha)
        p.end()
        self.preview = tinted
        self.preview_to_image = QTransform().translate(x / scale, y / scale).scale(1.0 / scale, 1.0 / scale)
        self.update()

    def set_lasso(self, points) -> None:
        self.lasso = QPolygonF(points) if points else None
        self.update()

    def set_busy(self, busy: bool, pos: QPointF | None = None) -> None:
        self.busy = busy
        if pos is not None:
            self.busy_pos = QPointF(pos)
        if busy:
            self._spinner.start()
        else:
            self._spinner.stop()
        self.update()

    def _tick(self) -> None:
        self._spin = (self._spin + 1) % 24
        r = QRectF(self.busy_pos.x() + 10, self.busy_pos.y() + 10, 22, 22)
        self.update(r.toAlignedRect().adjusted(-2, -2, 2, 2))

    # ------------------------------------------------------------ painting

    def paintEvent(self, _event) -> None:
        if self.preview is None and self.lasso is None and not self.busy:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.preview is not None:
            p.save()
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.setTransform(self.preview_to_image * self.image_to_widget)
            p.drawImage(0, 0, self.preview)
            p.restore()
        if self.lasso is not None:
            # Like Krita's Freehand Selection tool: the open path as drawn.
            # Dark underlay + light dashes stays visible on any artwork.
            path = self.image_to_widget.map(self.lasso)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(0, 0, 0, 160), 1.0))
            p.drawPolyline(path)
            dash = QPen(QColor(255, 255, 255, 230), 1.0, Qt.CustomDashLine)
            dash.setDashPattern([4, 4])
            p.setPen(dash)
            p.drawPolyline(path)
        if self.busy:
            c = self.busy_pos + QPointF(21, 21)
            for i in range(8):
                a = (i / 8.0) * 2 * math.pi + self._spin * (2 * math.pi / 24)
                alpha = int(60 + 195 * (((i - self._spin / 3) % 8) / 8.0))
                p.setPen(QPen(QColor(255, 255, 255, alpha), 2.2, Qt.SolidLine, Qt.RoundCap))
                inner = QPointF(c.x() + 4 * math.cos(a), c.y() + 4 * math.sin(a))
                outer = QPointF(c.x() + 8 * math.cos(a), c.y() + 8 * math.sin(a))
                p.drawLine(inner, outer)
        p.end()


class _PromptEdit(QLineEdit):
    """Line edit that reports the modifiers held with Return/Enter."""

    submitted = pyqtSignal(str, int)  # text, Qt.KeyboardModifiers as int
    cancelled = pyqtSignal()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            text = self.text().strip()
            if text:
                self.submitted.emit(text, int(event.modifiers() & ~Qt.KeypadModifier))
            event.accept()
            return
        if event.key() == Qt.Key_Escape:
            self.cancelled.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def event(self, event) -> bool:
        # Claim every plain key so Krita's single-letter tool shortcuts don't fire while typing.
        if event.type() == QEvent.ShortcutOverride and not (event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier)):
            event.accept()
            return True
        return super().event(event)


class PromptBar(QFrame):
    """Floating "type what to select" box, bottom-centre of the canvas."""

    submitted = pyqtSignal(str, int)

    MARGIN = 22

    def __init__(self, canvas: QWidget) -> None:
        super().__init__(canvas)
        self.setObjectName("SamSelectPromptBar")
        self.setAttribute(Qt.WA_NoMousePropagation)
        self.setAutoFillBackground(False)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 4, 4)
        layout.setSpacing(6)
        self.sparkle = QLabel("✦", self)
        self.edit = _PromptEdit(self)
        self.edit.setPlaceholderText(PLACEHOLDER)
        self.edit.setFrame(False)
        self.edit.setClearButtonEnabled(True)
        self.edit.setMinimumWidth(240)
        self.edit.setToolTip(
            "Describe what to select (e.g. “cat”, “red car”, “all windows”) and press Return.\n"
            "⇧↩ adds, ⌥↩ subtracts, ⇧⌥↩ intersects."
        )
        self.go = QToolButton(self)
        self.go.setText("↵")
        self.go.setAutoRaise(True)
        self.go.setToolTip("Select (Return)")
        self.status = QLabel("", self)
        self.status.hide()
        layout.addWidget(self.sparkle)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.status)
        layout.addWidget(self.go)
        self.edit.submitted.connect(self.submitted)
        self.edit.cancelled.connect(self._cancel)
        self.go.clicked.connect(self._go_clicked)
        self._restyle()
        canvas.installEventFilter(self)
        self.reposition()
        self.show()
        self.raise_()

    def _go_clicked(self) -> None:
        text = self.edit.text().strip()
        if text:
            self.submitted.emit(text, int(QApplication.keyboardModifiers()))

    def _cancel(self) -> None:
        if self.edit.text():
            self.edit.clear()
        else:
            parent = self.parentWidget()
            if parent is not None:
                parent.setFocus(Qt.OtherFocusReason)

    def show_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def _restyle(self) -> None:
        pal = QApplication.palette()
        bg = pal.window().color()
        fg = pal.windowText().color()
        dark = bg.lightness() < 128
        panel = QColor(bg).darker(115) if dark else QColor(bg).lighter(103)
        border = QColor(fg)
        border.setAlpha(60)
        accent = pal.highlight().color()
        sheet = (
            f"""
            QFrame#SamSelectPromptBar {{
                background: rgba({panel.red()},{panel.green()},{panel.blue()},235);
                border: 1px solid rgba({border.red()},{border.green()},{border.blue()},{border.alpha()});
                border-radius: 15px;
            }}
            QFrame#SamSelectPromptBar QLineEdit {{
                background: transparent; border: none; padding: 3px 2px; font-size: 13px;
                selection-background-color: {accent.name()};
            }}
            QFrame#SamSelectPromptBar QLabel {{ color: {accent.name()}; font-size: 13px; }}
            QFrame#SamSelectPromptBar QToolButton {{ border-radius: 11px; min-width: 22px; min-height: 22px; font-size: 13px; }}
            """
        )
        if sheet != self.styleSheet():
            self.setStyleSheet(sheet)

    def sizeHint(self) -> QSize:
        return QSize(380, 32)

    def reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        w = min(max(300, int(parent.width() * 0.38)), 520, parent.width() - 2 * self.MARGIN)
        h = self.sizeHint().height()
        self.setGeometry((parent.width() - w) // 2, parent.height() - h - self.MARGIN, w, h)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Resize:
            self.reposition()
        return False

    def changeEvent(self, event) -> None:
        # Krita theme switches arrive as an application palette change. (Not
        # StyleChange: setStyleSheet() itself emits that.)
        if event.type() == QEvent.ApplicationPaletteChange:
            self._restyle()
        super().changeEvent(event)

    def detach(self) -> None:
        parent = self.parentWidget()
        if parent is not None:
            parent.removeEventFilter(self)
        self.hide()
        self.deleteLater()
