"""Tool options for SAM Select, shown in Krita's Tool Options docker.

The first rows mirror Krita's own selection options (mode buttons with
Krita's icons, anti-aliasing, grow/shrink, feather, sample-layers reference)
and use Krita's slider spin boxes, so the panel reads like any other
selection tool. SAM-specific options and the backend status follow.
"""

from __future__ import annotations

from krita import Krita
from PyQt5 import sip
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import backend as bk
from . import modes, settings


def _slider(minimum: int, maximum: int, suffix: str, soft_max: int | None = None):
    """Krita's KisSliderSpinBox when available, else a plain QSpinBox."""
    holder = None
    try:
        from krita import SliderSpinBox

        holder = SliderSpinBox()
        holder.setRange(minimum, maximum)
        if soft_max is not None:
            holder.setSoftRange(max(minimum, -soft_max), soft_max)
        box = sip.cast(holder.widget(), QSpinBox)
    except Exception:  # noqa: BLE001 - older libkis
        box = QSpinBox()
        box.setRange(minimum, maximum)
    box.setSuffix(suffix)
    return holder, box


class ToolOptionsWidget(QWidget):
    modeChanged = pyqtSignal(str)
    installRequested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("SamSelectToolOptions")
        app = Krita.instance()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        outer.addLayout(form)

        # Mode: same icons and order as Krita's selection tools.
        mode_row = QHBoxLayout()
        mode_row.setSpacing(1)
        self.mode_group = QButtonGroup(self)
        self.mode_buttons = {}
        for m in modes.MODES:
            b = QToolButton(self)
            b.setCheckable(True)
            b.setAutoRaise(True)
            b.setIcon(app.icon(modes.ICONS[m]))
            b.setToolTip(modes.LABELS[m])
            self.mode_group.addButton(b)
            self.mode_buttons[m] = b
            mode_row.addWidget(b)
            b.clicked.connect(lambda _=False, m=m: self._set_mode(m))
        mode_row.addStretch(1)
        form.addRow("Mode:", mode_row)

        self.reference = QComboBox(self)
        self.reference.addItem(app.icon("all-layers"), "All Layers", settings.SAMPLE_ALL)
        self.reference.addItem(app.icon("current-layer"), "Current Layer", settings.SAMPLE_CURRENT)
        self.reference.setToolTip("Which pixels SAM looks at: the whole image, or only the active layer.")
        form.addRow("Reference:", self.reference)

        self.antialias = QCheckBox("Anti-aliasing", self)
        self.antialias.setToolTip("Smooth the selection edge (about one pixel of partial selection).")
        form.addRow("", self.antialias)

        self._grow_holder, self.grow = _slider(-400, 400, " px", soft_max=40)
        self.grow.setToolTip("Grow (positive) or shrink (negative) the new selection before combining it.")
        form.addRow("Grow:", self.grow)
        self._feather_holder, self.feather = _slider(0, 400, " px", soft_max=40)
        self.feather.setToolTip("Feather the new selection's edge before combining it.")
        form.addRow("Feather:", self.feather)

        # SAM-specific.
        self.lasso_mode = QComboBox(self)
        self.lasso_mode.addItem("Everything it covers", True)
        self.lasso_mode.addItem("Best single match", False)
        self.lasso_mode.setToolTip(
            "The lasso is a rough hint, not a boundary: it doesn't need to enclose anything.\n"
            "Everything it covers: the objects that together best match the area you drew.\n"
            "Best single match: only the one object that best matches it."
        )
        form.addRow("Lasso selects:", self.lasso_mode)

        self.threshold = QComboBox(self)
        for label, value in (("Strict", 0.7), ("Normal", 0.5), ("Loose", 0.35), ("Very loose", 0.2)):
            self.threshold.addItem(label, value)
        self.threshold.setToolTip("How confident SAM must be before a text match is selected.")
        form.addRow("Text match:", self.threshold)

        self.hover = QCheckBox("Highlight object under cursor", self)
        form.addRow("", self.hover)

        # Backend status.
        status_row = QHBoxLayout()
        self.status_dot = QLabel("●", self)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        status_row.addWidget(self.status_dot, 0, Qt.AlignTop)
        status_row.addWidget(self.status, 1)
        outer.addLayout(status_row)
        self.license = QLabel(
            f'The model is Meta\'s SAM 3, licensed under the <a href="{bk.SAM_LICENSE_URL}">SAM License</a>. '
            "Installing it means you accept that license.",
            self,
        )
        self.license.setWordWrap(True)
        self.license.setOpenExternalLinks(True)
        self.license.setTextInteractionFlags(Qt.TextBrowserInteraction)
        outer.addWidget(self.license)
        self.progress = QProgressBar(self)
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(6)
        outer.addWidget(self.progress)
        buttons = QHBoxLayout()
        self.action_button = QPushButton(self)
        self.log_button = QPushButton("Log", self)
        self.log_button.setToolTip(str(bk.Backend.instance().log_path()))
        buttons.addWidget(self.action_button, 1)
        buttons.addWidget(self.log_button)
        outer.addLayout(buttons)

        self.hint = QLabel(self)
        self.hint.setWordWrap(True)
        self.hint.setEnabled(False)  # renders in the muted text colour
        outer.addWidget(self.hint)
        outer.addStretch(1)

        self._load()
        self.reference.currentIndexChanged.connect(lambda _: settings.put("sampleLayersMode", self.reference.currentData()))
        self.antialias.toggled.connect(lambda v: settings.put("antiAliasSelection", v))
        self.grow.valueChanged.connect(lambda v: settings.put("growSelection", v))
        self.feather.valueChanged.connect(lambda v: settings.put("featherSelection", v))
        self.lasso_mode.currentIndexChanged.connect(lambda _: settings.put("lassoSelectsAllObjects", self.lasso_mode.currentData()))
        self.threshold.currentIndexChanged.connect(lambda _: settings.put("textThreshold", self.threshold.currentData()))
        self.hover.toggled.connect(lambda v: settings.put("hoverPreview", v))
        self.action_button.clicked.connect(self._on_action)
        self.log_button.clicked.connect(self._open_log)
        backend = bk.Backend.instance()
        backend.stateChanged.connect(self.show_state)
        self.show_state(backend.state, backend.message, backend.progress)

    def _load(self) -> None:
        self.set_mode(settings.mode())
        self.reference.setCurrentIndex(max(0, self.reference.findData(settings.get("sampleLayersMode"))))
        self.antialias.setChecked(settings.get("antiAliasSelection"))
        self.grow.setValue(settings.get("growSelection"))
        self.feather.setValue(settings.get("featherSelection"))
        self.lasso_mode.setCurrentIndex(0 if settings.get("lassoSelectsAllObjects") else 1)
        threshold = settings.get("textThreshold")
        idx = min(range(self.threshold.count()), key=lambda i: abs(self.threshold.itemData(i) - threshold))
        self.threshold.setCurrentIndex(idx)
        self.hover.setChecked(settings.get("hoverPreview"))
        swap = settings.swap_ctrl_alt()
        self.hint.setText(
            "Click an object to select it · roughly lasso objects to select them (no need to enclose them) · "
            "type in the box on the canvas to select by description.\n" + modes.modifier_hint(swap)
        )

    def reload(self) -> None:
        """Re-read settings (e.g. another selection tool changed the shared mode)."""
        widgets = [self.reference, self.antialias, self.grow, self.feather, self.lasso_mode, self.threshold, self.hover]
        for w in widgets:
            w.blockSignals(True)
        try:
            self._load()
        finally:
            for w in widgets:
                w.blockSignals(False)

    def set_mode(self, mode: str) -> None:
        self.mode_buttons[mode].setChecked(True)

    def _set_mode(self, mode: str) -> None:
        settings.set_mode(mode)
        self.modeChanged.emit(mode)

    # ------------------------------------------------------------ backend

    def show_state(self, state: str, message: str, progress: float) -> None:
        colors = {bk.READY: "#3bb273", bk.ERROR: "#e05d5d", bk.MISSING: "#e0a030"}
        self.status_dot.setStyleSheet(f"color: {colors.get(state, '#8a8a8a')};")
        default = {
            bk.MISSING: "SAM 3 isn't set up yet. One-time install: ~350 MB of Python packages, then a 1.7 GB model download.",
            bk.STOPPED: "SAM 3 is not running. It starts automatically when you use the tool.",
        }.get(state, "")
        self.status.setText(message or default)
        self.license.setVisible(state == bk.MISSING or state == bk.INSTALLING)
        busy = state in bk.BUSY_STATES
        self.progress.setVisible(busy)
        if busy:
            if progress < 0:
                self.progress.setRange(0, 0)
            else:
                self.progress.setRange(0, 1000)
                self.progress.setValue(int(progress * 1000))
        label = {
            bk.MISSING: "Install SAM 3…",
            bk.ERROR: "Retry",
            bk.STOPPED: "Start",
            bk.READY: "Unload",
        }.get(state)
        self.action_button.setVisible(label is not None)
        if label:
            self.action_button.setText(label)

    def _on_action(self) -> None:
        backend = bk.Backend.instance()
        if backend.state == bk.MISSING or (backend.state == bk.ERROR and not backend.is_installed()):
            self.installRequested.emit()
        elif backend.state == bk.READY:
            backend.stop()
        else:
            backend.ensure_started()

    def _open_log(self) -> None:
        from PyQt5.QtCore import QUrl
        from PyQt5.QtGui import QDesktopServices

        path = bk.Backend.instance().log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


class ToolOptionsHost:
    """Temporarily shows a widget in Krita's Tool Options docker.

    KoToolDocker keeps the active tool's widgets in a QGridLayout nested at
    index 0 of the scroll area's box layout. We insert ours above it and hide
    the current tool's widgets; Krita never touches that box layout, and the
    next tool switch rebuilds the grid anyway.
    """

    def __init__(self, qwindow) -> None:
        self.qwindow = qwindow
        self._hidden: list[QWidget] = []
        self._widget: QWidget | None = None
        self._box = None

    def _layouts(self):
        dock = self.qwindow.findChild(QDockWidget, "sharedtooldocker")
        if dock is None or dock.widget() is None:
            return None, None
        area = dock.widget()
        inner = area.widget() if hasattr(area, "widget") else None
        box = inner.layout() if inner is not None else None
        if box is None or box.count() == 0:
            return None, None
        grid = box.itemAt(0).layout()
        return box, grid

    def show(self, widget: QWidget) -> bool:
        box, grid = self._layouts()
        if box is None:
            return False
        self._hidden = []
        if grid is not None:
            for i in range(grid.count()):
                w = grid.itemAt(i).widget()
                if w is not None and w.isVisible():
                    w.hide()
                    self._hidden.append(w)
        box.insertWidget(0, widget)
        widget.show()
        self._widget, self._box = widget, box
        return True

    def hide(self) -> None:
        if self._widget is not None and self._box is not None and not sip.isdeleted(self._widget):
            self._box.removeWidget(self._widget)
            self._widget.hide()
            self._widget.setParent(None)
        for w in self._hidden:
            if not sip.isdeleted(w):
                w.show()
        self._hidden, self._widget, self._box = [], None, None
