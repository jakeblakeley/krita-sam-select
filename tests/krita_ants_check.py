"""Diagnostic: are marching ants drawn for SAM Select selections?

Compares a SAM Select selection with selections made by Krita's own actions,
capturing each both with QWidget.grab() and from the on-screen window.
Run like tests/krita_selftest.py (via SAMSELECT_SELFTEST).
"""

import os
import time
from pathlib import Path

from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt, QTimer
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtWidgets import QApplication

from samselect import backend as bk
from samselect import canvas as cv
from samselect import settings

OUT = Path(os.environ.get("SAMSELECT_SELFTEST_OUT", "/tmp/samselect-selftest"))
IMAGE = os.environ.get("SAMSELECT_SELFTEST_IMAGE", "")
OUT.mkdir(parents=True, exist_ok=True)
app = Krita.instance()  # noqa: F821
tool = tool  # noqa: F821,PLW0127
log = []
hover_was = settings.get("hoverPreview")
settings.put("hoverPreview", False)


def shots(name):
    tool.canvas.grab().save(str(OUT / f"{name}_grab.png"))
    screen = QApplication.primaryScreen()
    screen.grabWindow(int(tool.qwindow.winId())).save(str(OUT / f"{name}_screen.png"))
    s = app.activeDocument().selection()
    log.append(f"{name}: selection={None if s is None else (s.x(), s.y(), s.width(), s.height())}")


def click(x, y):
    p = cv.image_to_widget(tool.view).map(QPointF(x, y))
    g = QPointF(tool.canvas.mapToGlobal(QPoint(int(p.x()), int(p.y()))))
    for kind, buttons in ((QEvent.MouseButtonPress, Qt.LeftButton), (QEvent.MouseButtonRelease, Qt.NoButton)):
        QApplication.sendEvent(tool.canvas, QMouseEvent(kind, p, g, Qt.LeftButton, buttons, Qt.NoModifier))


def steps():
    doc = app.openDocument(IMAGE)
    app.activeWindow().addView(doc)
    yield 2
    tool.activate()
    backend = bk.Backend.instance()
    yield lambda: backend.state == bk.READY and tool._capture is not None and backend.has_image(tool._capture.key)
    click(doc.width() * 0.5, doc.height() * 0.5)
    yield lambda: doc.selection() is not None and tool._pending == 0
    yield 2
    shots("1_sam")
    app.action("invert_selection").trigger()
    yield 1
    app.action("invert_selection").trigger()
    yield 2
    shots("2_sam_after_krita_invert_x2")
    app.action("deselect").trigger()
    yield 1
    app.action("select_all").trigger()
    yield 2
    shots("3_krita_select_all")


def finish():
    settings.put("hoverPreview", hover_was)
    (OUT / "ants_log.txt").write_text("\n".join(log))
    for d in app.documents():
        d.setModified(False)
    QTimer.singleShot(300, lambda: app.action("file_quit").trigger())


def drive(gen):
    try:
        step = next(gen)
    except StopIteration:
        finish()
        return
    except Exception as exc:  # noqa: BLE001
        log.append(f"error: {exc!r}")
        finish()
        return
    if callable(step):
        limit = time.time() + 90

        def poll():
            if step() or time.time() > limit:
                drive(gen)
            else:
                QTimer.singleShot(50, poll)

        poll()
    else:
        QTimer.singleShot(int(step * 1000), lambda: drive(gen))


drive(steps())
