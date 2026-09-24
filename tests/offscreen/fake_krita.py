"""A stand-in for Krita's `krita` module and main-window widget tree.

Reproduces the structures the plugin relies on (as found in Krita 5.3's
source): the ToolBox dock with a KoToolBox, named Sections and one exclusive
QButtonGroup; the Tool Options dock ("sharedtooldocker") with its scroll area
-> housekeeper -> box layout -> grid; and an MDI area whose sub-window holds a
scroll area with a KisOpenGLCanvas2 in its viewport. Enough of libkis is
faked (View transforms, Document pixels, Selection) to drive the plugin.
"""

from __future__ import annotations

import sys
import types

from PyQt5.QtCore import QObject, QRect, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QImage, QPainter, QTransform
from PyQt5.QtWidgets import (
    QAbstractScrollArea,
    QAction,
    QButtonGroup,
    QDockWidget,
    QGridLayout,
    QLabel,
    QLayout,
    QMainWindow,
    QMdiArea,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

BUTTON = 32  # icon 22 + KoToolBox BUTTON_MARGIN 10


# ----------------------------------------------------------------- libkis


class Selection(QObject):
    """Records operations; keeps pixels as a dict for inspection."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ops = []
        self.rect = QRect()
        self.bound = False

    def setPixelData(self, data, x, y, w, h):
        self.ops.append(("setPixelData", x, y, w, h, len(bytes(data))))
        self.rect = QRect(x, y, w, h)

    def duplicate(self):
        s = Selection()
        s.rect = QRect(self.rect)
        s.bound = self.bound
        s.ops = [("duplicate",)]
        return s

    def width(self):
        return self.rect.width()

    def height(self):
        return self.rect.height()

    def __getattr__(self, name):
        if name in ("add", "subtract", "intersect", "symmetricdifference", "grow", "shrink", "feather", "invert", "select", "replace"):
            def op(*args):
                self.ops.append((name,) + tuple(a if not isinstance(a, Selection) else "Selection" for a in args))
                if name == "add" and args:
                    self.rect = self.rect.united(args[0].rect)
            return op
        raise AttributeError(name)


class SelectionMask:
    def setSelection(self, sel):
        sel.bound = True


class Document(QObject):
    def __init__(self, image: QImage):
        super().__init__()
        self.image = image
        self.selections = []  # every setSelection call

    def width(self):
        return self.image.width()

    def height(self):
        return self.image.height()

    def projection(self, x, y, w, h):
        return self.image.copy(x, y, w, h).convertToFormat(QImage.Format_ARGB32)

    def thumbnail(self, w, h):
        return self.image.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def activeNode(self):
        return None

    def selection(self):
        return self.selections[-1] if self.selections and self.selections[-1] is not None else None

    def setSelection(self, sel):
        self.selections.append(sel)

    def createSelectionMask(self, name):
        return SelectionMask()


class View(QObject):
    def __init__(self, doc, zoom=0.5, offset=(40, 30)):
        super().__init__()
        self._doc = doc
        self.zoom, self.offset = zoom, offset
        self.messages = []

    def document(self):
        return self._doc

    # Krita: flake = document points; keep it simple: flake == image pixels.
    def flakeToImageTransform(self):
        return QTransform()

    def flakeToCanvasTransform(self):
        return QTransform().translate(*self.offset).scale(self.zoom, self.zoom)

    def showFloatingMessage(self, text, icon, ms, priority):
        self.messages.append(text)


class Window(QObject):
    activeViewChanged = pyqtSignal()
    themeChanged = pyqtSignal()
    windowClosed = pyqtSignal()

    def __init__(self, qwindow, view):
        super().__init__()
        self._q, self._view = qwindow, view

    def qwindow(self):
        return self._q

    def activeView(self):
        return self._view


class Notifier(QObject):
    windowCreated = pyqtSignal()
    applicationClosing = pyqtSignal()

    def setActive(self, value):
        pass


class Krita(QObject):
    _instance = None

    def __init__(self):
        super().__init__()
        self.settings = {}
        self._notifier = Notifier()
        self.window = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = Krita()
        return cls._instance

    def readSetting(self, group, key, default):
        return self.settings.get((group, key), default)

    def writeSetting(self, group, key, value):
        self.settings[(group, key)] = value

    def icon(self, name):
        return QIcon()

    def action(self, name):
        return self.window.findChild(QAction, name) if self.window is not None else None

    def notifier(self):
        return self._notifier

    def windows(self):
        return []


class Extension(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)


def install_module():
    mod = types.ModuleType("krita")
    for name in ("Krita", "Extension", "Selection"):
        setattr(mod, name, globals()[name])
    sys.modules["krita"] = mod


# ---------------------------------------------------------- widget tree


class SectionLayout(QLayout):
    """Like Krita's: positions only its own buttons, ignores addItem."""

    def __init__(self, parent):
        super().__init__(parent)
        self.buttons = []

    def addItem(self, item):  # Q_ASSERT(0) in Krita; a no-op in release builds
        pass

    def count(self):
        return 0

    def itemAt(self, i):
        return None

    def takeAt(self, i):
        return None

    def sizeHint(self):
        return QSize()

    def setGeometry(self, rect):
        super().setGeometry(rect)
        cols = max(1, rect.width() // BUTTON)
        for i, b in enumerate(self.buttons):
            r, c = divmod(i, cols)
            b.setGeometry(c * BUTTON, r * BUTTON, BUTTON, BUTTON)


class KoToolBoxLayout(QLayout):
    def __init__(self, parent, sections):
        super().__init__(parent)
        self.sections = sections

    def addItem(self, item):
        pass

    def count(self):
        return 0

    def itemAt(self, i):
        return None

    def takeAt(self, i):
        return None

    def sizeHint(self):
        return QSize(2 * BUTTON, self.heightForWidth(2 * BUTTON))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        cols = max(1, w // BUTTON)
        return sum(-(-len(s.layout().buttons) // cols) * BUTTON + 6 for s in self.sections)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        cols = max(1, rect.width() // BUTTON)
        y = 0
        for s in self.sections:
            n = len(s.layout().buttons)
            used = min(cols, n)
            rows = -(-n // cols)
            s.setGeometry(0, y, used * BUTTON, rows * BUTTON)
            y += rows * BUTTON + 6


class KoToolBox(QWidget):
    pass


class KisOpenGLCanvas2(QWidget):
    pass


def build_main_window(image: QImage):
    win = QMainWindow()
    win.resize(1400, 900)

    # Toolbox.
    toolbox = KoToolBox()
    group = QButtonGroup(toolbox)
    group.setExclusive(True)
    specs = {
        "main": ["KritaShape/KisToolBrush", "KritaFill/KisToolFill", "KritaSelected/KisToolColorSampler"],
        "5 Krita/Select": [
            "KisToolSelectRectangular", "KisToolSelectElliptical", "KisToolSelectPolygonal", "KisToolSelectOutline",
            "KisToolSelectContiguous", "KisToolSelectSimilar", "KisToolSelectPath", "KisToolSelectMagnetic",
        ],
        "navigation": ["PanTool", "ZoomTool"],
    }
    sections = []
    for name, tools in specs.items():
        sec = QWidget(toolbox)
        sec.setObjectName(name)
        lay = SectionLayout(sec)
        sec.setLayout(lay)
        for tid in tools:
            b = QToolButton(sec)
            b.setObjectName(tid)
            b.setCheckable(True)
            b.setIconSize(QSize(22, 22))
            b.resize(BUTTON, BUTTON)
            group.addButton(b)
            lay.buttons.append(b)
            act = QAction(win)
            act.setObjectName(tid)
            win.addAction(act)
        sections.append(sec)
    toolbox.setLayout(KoToolBoxLayout(toolbox, sections))
    area = QScrollArea()
    area.setWidget(toolbox)
    area.setWidgetResizable(True)
    dock = QDockWidget("Toolbox", win)
    dock.setObjectName("ToolBox")
    dock.setWidget(area)
    win.addDockWidget(Qt.LeftDockWidgetArea, dock)
    toolbox.resize(2 * BUTTON, toolbox.layout().heightForWidth(2 * BUTTON))

    # Tool options docker, structured like KoToolDocker.
    house = QWidget()
    box = QVBoxLayout(house)
    grid = QGridLayout()
    tool_label = QLabel("contiguous selection options")
    tool_label.setObjectName("contiguous-options")
    grid.addWidget(tool_label, 0, 0)
    box.addLayout(grid)
    box.addStretch(1)
    opts_area = QScrollArea()
    opts_area.setWidgetResizable(True)
    opts_area.setWidget(house)
    opts = QDockWidget("Tool Options", win)
    opts.setObjectName("sharedtooldocker")
    opts.setWidget(opts_area)
    win.addDockWidget(Qt.RightDockWidgetArea, opts)

    # Canvas.
    mdi = QMdiArea()
    view_root = QWidget()
    controller = QAbstractScrollArea(view_root)
    canvas = KisOpenGLCanvas2(controller.viewport())
    canvas.setMouseTracking(True)
    lay = QVBoxLayout(view_root)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(controller)
    sub = mdi.addSubWindow(view_root)
    sub.showMaximized()
    win.setCentralWidget(mdi)

    for name in ("selection_tool_mode_add", "selection_tool_mode_subtract", "selection_tool_mode_intersect", "selection_tool_mode_replace", "samselect_tool"):
        act = QAction(win)
        act.setObjectName(name)
        win.addAction(act)

    win.show()
    canvas.setGeometry(controller.viewport().rect())
    return win, toolbox, canvas, tool_label


def test_image() -> QImage:
    """Two discs on a gradient, 1200 x 800."""
    img = QImage(1200, 800, QImage.Format_ARGB32)
    p = QPainter(img)
    for y in range(0, 800, 8):
        p.fillRect(0, y, 1200, 8, QColor(80 + y // 10, 90 + y // 12, 110))
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(220, 40, 40))
    p.setPen(Qt.NoPen)
    p.drawEllipse(200, 250, 300, 300)
    p.setBrush(QColor(40, 70, 220))
    p.drawEllipse(750, 280, 240, 240)
    p.end()
    return img
