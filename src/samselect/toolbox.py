"""A SAM Select button inside Krita's toolbox, next to the selection tools.

Python cannot register real Krita tools (KoToolFactoryBase isn't exposed),
so the button is injected into the toolbox widget tree:

* it joins KoToolBox's exclusive QButtonGroup, so checking it unchecks the
  previous tool and clicking any Krita tool unchecks it;
* it sits in the next free cell of the "5 Krita/Select" section. Krita's
  SectionLayout/KoToolBoxLayout ignore foreign widgets (their addItem is a
  no-op), so after every Krita layout pass we place the button ourselves and,
  when the section's last row is full, open a new row by pushing the
  following sections (and their separators) down;
* it mimics KoToolBoxButton: auto-raise, checkable, same icon size, and the
  highlight-colour palette Krita uses for the active tool.
"""

from __future__ import annotations

from PyQt5.QtCore import QEvent, QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QPalette
from PyQt5.QtWidgets import QApplication, QButtonGroup, QDockWidget, QToolButton, QWidget

SELECT_SECTION = "5 Krita/Select"
BUTTON_NAME = "SamSelectToolButton"


def set_highlight(button: QToolButton) -> None:
    """Same palette trick as KoToolBoxButton::setHighlightColor()."""
    pal = QApplication.palette()
    if button.isChecked():
        pal = QPalette(pal)
        pal.setColor(QPalette.Button, pal.color(QPalette.Highlight))
    button.setPalette(pal)


def _class_name(obj: QObject) -> str:
    return obj.metaObject().className()


class ToolboxButton(QObject):
    toggled = pyqtSignal(bool)

    def __init__(self, qwindow, icon, tooltip: str) -> None:
        super().__init__(qwindow)
        self.toolbox = None
        self.section = None
        self.group = None
        self.button = None
        self._extra = 0  # px added to the section by us in the current layout
        self._pending = False
        dock = qwindow.findChild(QDockWidget, "ToolBox")
        if dock is None:
            return
        self.toolbox = next((w for w in dock.findChildren(QWidget) if _class_name(w) == "KoToolBox"), None)
        if self.toolbox is None:
            return
        self.group = self.toolbox.findChild(QButtonGroup)
        self.section = self.toolbox.findChild(QWidget, SELECT_SECTION)
        parent = self.section if self.section is not None else self.toolbox
        btn = QToolButton(parent)
        btn.setObjectName(BUTTON_NAME)
        btn.setCheckable(True)
        btn.setAutoRaise(True)
        btn.setIcon(icon)
        btn.setToolTip(tooltip)
        btn.setFocusPolicy(Qt.NoFocus)
        btn.toggled.connect(self._on_toggled)
        self.button = btn
        if self.group is not None:
            self.group.addButton(btn)
        # Re-place the button whenever Krita lays the toolbox out again.
        self.toolbox.installEventFilter(self)
        for sec in self._sections():
            sec.installEventFilter(self)
        self.schedule()

    @property
    def ok(self) -> bool:
        return self.button is not None

    # ------------------------------------------------------------ state

    def _on_toggled(self, checked: bool) -> None:
        set_highlight(self.button)
        if checked and self.group is not None:
            # The tool that was active keeps Krita's highlight palette unless we reset it.
            for b in self.group.buttons():
                if b is not self.button:
                    b.setPalette(QApplication.palette())
        self.toggled.emit(checked)

    def set_checked(self, checked: bool) -> None:
        self.button.setChecked(checked)

    def check_krita_button(self, tool_id: str) -> None:
        """Visually return to a Krita tool (used when Krita won't emit changedTool)."""
        if self.toolbox is None:
            return
        btn = self.toolbox.findChild(QToolButton, tool_id)
        if btn is not None:
            btn.setChecked(True)
            set_highlight(btn)

    def set_icon(self, icon) -> None:
        if self.button is not None:
            self.button.setIcon(icon)

    # ------------------------------------------------------------ layout

    def _sections(self) -> list:
        if self.toolbox is None:
            return []
        return [w for w in self.toolbox.children() if isinstance(w, QWidget) and w.objectName() and w.layout() is not None]

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.LayoutRequest, QEvent.Resize, QEvent.Move, QEvent.Show):
            self.schedule()
        return False

    def schedule(self) -> None:
        if not self._pending:
            self._pending = True
            QTimer.singleShot(0, self.relayout)

    def _reference_button(self):
        if self.section is None:
            return None
        for b in self.section.findChildren(QToolButton):
            if b is not self.button and b.isVisible():
                return b
        return None

    def relayout(self) -> None:
        self._pending = False
        if self.button is None or self.toolbox is None:
            return
        ref = self._reference_button()
        if ref is None:
            self.button.hide()
            return
        self.button.setIconSize(ref.iconSize())
        bw, bh = ref.width(), ref.height()
        sec = self.section
        count = sum(1 for b in sec.findChildren(QToolButton) if b is not self.button and not b.isHidden())
        vertical = self.toolbox.height() >= self.toolbox.width()
        if vertical:
            cols = max(1, sec.width() // bw)
            row, col = divmod(count, cols)
            natural = -(-count // cols) * bh  # ceil(count / cols) rows
            need = bh if row * bh >= natural else 0
        else:
            rows = max(1, sec.height() // bh)
            col, row = divmod(count, rows)
            natural = -(-count // rows) * bw
            need = bw if col * bw >= natural else 0

        # Krita's layout pass resets the section to its natural size; detect that.
        current = sec.height() if vertical else sec.width()
        if current == natural:
            self._extra = 0
        if need and self._extra == 0:
            self._extra = need
            if vertical:
                sec.resize(sec.width(), natural + need)
                for other in self._sections():
                    if other is not sec and other.y() > sec.y():
                        other.move(other.x(), other.y() + need)
            else:
                sec.resize(natural + need, sec.height())
                for other in self._sections():
                    if other is not sec and other.x() > sec.x():
                        other.move(other.x() + need, other.y())
        if vertical:
            target = self.toolbox.layout().heightForWidth(self.toolbox.width()) + self._extra
            if self._extra and self.toolbox.height() < target:
                self.toolbox.resize(self.toolbox.width(), target)
        self.button.setGeometry(col * bw, row * bh, bw, bh)
        self.button.show()
        self.button.raise_()
        self.toolbox.update()  # separators are painted from section geometry

    def remove(self) -> None:
        if self.toolbox is not None:
            self.toolbox.removeEventFilter(self)
            for sec in self._sections():
                sec.removeEventFilter(self)
        if self.button is not None:
            if self.group is not None:
                self.group.removeButton(self.button)
            self.button.deleteLater()
            self.button = None
        if self.toolbox is not None and self.toolbox.layout() is not None:
            self.toolbox.layout().invalidate()
