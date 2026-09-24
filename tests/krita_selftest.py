"""In-Krita integration test, run through the plugin's developer hook.

    SAMSELECT_SELFTEST=$PWD/tests/krita_selftest.py \
    SAMSELECT_SELFTEST_IMAGE=/path/to/truck.jpg \
    SAMSELECT_SELFTEST_OUT=/tmp/samselect-selftest \
    /Applications/krita.app/Contents/MacOS/krita --nosplash

Opens the image, drives SAM Select with synthetic input on the real canvas,
checks the resulting selections (bbox, undo/redo, modes), saves screenshots
and a report.json to SAMSELECT_SELFTEST_OUT, then quits Krita without saving.
Executed by extension._run_selftest with ``tool`` and ``Krita`` in scope.
"""

import json
import math
import os
import time
import traceback
from pathlib import Path

from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt, QTimer
from PyQt5.QtGui import QKeyEvent, QMouseEvent
from PyQt5.QtWidgets import QApplication, QDockWidget, QToolButton

from samselect import backend as bk
from samselect import canvas as cv
from samselect import settings

OUT = Path(os.environ.get("SAMSELECT_SELFTEST_OUT", "/tmp/samselect-selftest"))
IMAGE = os.environ.get("SAMSELECT_SELFTEST_IMAGE", "")
OUT.mkdir(parents=True, exist_ok=True)
report = {"checks": [], "timings": {}, "notes": []}
app = Krita.instance()  # noqa: F821 - injected
tool = tool  # noqa: F821,PLW0127 - injected


def check(name, cond, detail=""):
    report["checks"].append({"name": name, "ok": bool(cond), "detail": str(detail)})


def shot(name, widget=None):
    try:
        (widget or tool.qwindow).grab().save(str(OUT / f"{name}.png"))
    except Exception as exc:  # noqa: BLE001
        report["notes"].append(f"screenshot {name} failed: {exc}")


def sel_rect(doc):
    s = doc.selection()
    return None if s is None else (s.x(), s.y(), s.width(), s.height())


def click(img_x, img_y, mods=Qt.NoModifier):
    tf = cv.image_to_widget(tool.view)
    p = tf.map(QPointF(img_x, img_y))
    g = tool.canvas.mapToGlobal(QPoint(int(p.x()), int(p.y())))
    for kind, buttons in ((QEvent.MouseButtonPress, Qt.LeftButton), (QEvent.MouseButtonRelease, Qt.NoButton)):
        QApplication.sendEvent(tool.canvas, QMouseEvent(kind, p, QPointF(g), Qt.LeftButton, buttons, mods))


HOVER_WAS = None


def steps():
    global HOVER_WAS
    HOVER_WAS = settings.get("hoverPreview")
    doc = app.openDocument(IMAGE)
    app.activeWindow().addView(doc)
    yield 2.0
    W, H = doc.width(), doc.height()
    report["image"] = [W, H]

    dock = tool.qwindow.findChild(QDockWidget, "ToolBox")
    shot("toolbox", dock)
    btn = tool.toolbox.button
    check("toolbox button injected", btn is not None and btn.isVisible(), btn.geometry() if btn else None)
    if btn is not None:
        check("button is in the Select section", btn.parentWidget().objectName() == "5 Krita/Select", btn.parentWidget().objectName())

    settings.put("hoverPreview", False)
    t0 = time.time()
    btn.click()
    yield 0.3
    check("tool active", tool.active and btn.isChecked())
    wand = tool.toolbox.toolbox.findChild(QToolButton, "KisToolSelectContiguous")
    check("contiguous button unchecked", wand is not None and not wand.isChecked())
    check("prompt bar", tool.prompt is not None and tool.prompt.isVisible())
    check("options in Tool Options docker", tool.options.isVisible())
    backend = bk.Backend.instance()
    yield lambda: backend.state in (bk.READY, bk.ERROR, bk.MISSING)
    report["timings"]["backend_ready_s"] = round(time.time() - t0, 2)
    check("backend ready", backend.state == bk.READY, f"{backend.state}: {backend.message}")
    if backend.state != bk.READY:
        return
    yield lambda: tool._capture is not None and backend.has_image(tool._capture.key)
    report["timings"]["prefetch_done_s"] = round(time.time() - t0, 2)
    shot("activated")

    # 1. click the truck body (replace)
    t = time.time()
    click(W * 0.35, H * 0.45)
    yield lambda: doc.selection() is not None and tool._pending == 0
    report["timings"]["click_to_selection_ms"] = round((time.time() - t) * 1000)
    r1 = sel_rect(doc)
    check("click selected something", r1 is not None and r1[2] > 50, r1)
    yield 1.5  # let Krita compute the marching-ants outline
    shot("after_click")

    # 1b. Krita's selection actions bar (if enabled) stays on top and keeps its input
    tool.canvas.repaint()
    yield 0.2
    bar_widgets = [w for w in tool.canvas.children() if cv.is_actions_bar_widget(w) and w.isVisible()]
    report["actions_bar"] = {"widgets": len(bar_widgets), "exclude": str(tool.overlay.exclude)}
    if bar_widgets:
        check("overlay excludes the actions bar", tool.overlay.exclude is not None and tool.overlay.exclude.contains(bar_widgets[0].geometry()))
        hit = tool.canvas.childAt(bar_widgets[0].geometry().center())
        check("actions bar button is what the pointer hits", hit is bar_widgets[0], type(hit).__name__)
        check("text box clear of the actions bar", not tool.prompt.geometry().intersects(tool.overlay.exclude))
    else:
        report["notes"].append("selection actions bar not shown (disabled in Krita's settings?)")

    # 2. undo / redo
    app.action("edit_undo").trigger()
    yield 0.8
    r_undo = sel_rect(doc)
    check("undo removes the selection", r_undo is None or r_undo[2] == 0, r_undo)
    app.action("edit_redo").trigger()
    yield 0.8
    check("redo restores it", sel_rect(doc) == r1, sel_rect(doc))

    # 3. text prompt, shift+return (add)
    tool.prompt.edit.setFocus()
    tool.prompt.edit.setText("tire")
    t = time.time()
    n_before = sel_rect(doc)
    QApplication.sendEvent(tool.prompt.edit, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.ShiftModifier))
    yield 0.05
    yield lambda: tool._pending == 0
    report["timings"]["text_to_selection_ms"] = round((time.time() - t) * 1000)
    check("text prompt status", tool.prompt.status.text().endswith("found"), tool.prompt.status.text())
    r_text = sel_rect(doc)
    check("shift+return added (bbox grew or equal)", r_text is not None and r_text[2] >= n_before[2], (n_before, r_text))
    yield 1.5
    shot("after_text_add")

    # 4. option+click subtract a region
    click(W * 0.35, H * 0.45, Qt.AltModifier)
    yield 0.05
    yield lambda: tool._pending == 0
    yield 1.0
    shot("after_subtract")
    report["after_subtract"] = sel_rect(doc)

    # 5. freehand lasso around the whole truck (replace)
    tf = cv.image_to_widget(tool.view)
    ring = [(W * (0.5 + 0.49 * math.cos(a)), H * (0.47 + 0.27 * math.sin(a))) for a in (i * 2 * math.pi / 60 for i in range(61))]
    pts = [tf.map(QPointF(x, y)) for x, y in ring]

    def mouse(kind, p, buttons):
        g = QPointF(tool.canvas.mapToGlobal(QPoint(int(p.x()), int(p.y()))))
        QApplication.sendEvent(tool.canvas, QMouseEvent(kind, p, g, Qt.LeftButton, buttons, Qt.NoModifier))

    mouse(QEvent.MouseButtonPress, pts[0], Qt.LeftButton)
    for p in pts[1:]:
        mouse(QEvent.MouseMove, p, Qt.LeftButton)
    shot("lasso_drawing")
    t = time.time()  # release -> selection
    mouse(QEvent.MouseButtonRelease, pts[-1], Qt.NoButton)
    yield 0.05
    yield lambda: tool._pending == 0
    report["timings"]["lasso_to_selection_ms"] = round((time.time() - t) * 1000)
    r_lasso = sel_rect(doc)
    report["lasso"] = r_lasso
    check("lasso selects the truck", r_lasso is not None and r_lasso[2] > W * 0.8 and r_lasso[3] > H * 0.4, r_lasso)
    yield 1.0
    shot("after_lasso")

    # 6. hover preview
    settings.put("hoverPreview", True)
    tf = cv.image_to_widget(tool.view)
    p = tf.map(QPointF(W * 0.62, H * 0.52))
    QApplication.sendEvent(tool.canvas, QMouseEvent(QEvent.MouseMove, p, QPointF(tool.canvas.mapToGlobal(QPoint(int(p.x()), int(p.y())))), Qt.NoButton, Qt.NoButton, Qt.NoModifier))
    yield lambda: tool.overlay is not None and tool.overlay.preview is not None
    check("hover preview", tool.overlay.preview is not None)
    shot("hover")

    # 7. leave via another tool
    brush = tool.toolbox.toolbox.findChild(QToolButton, "KritaShape/KisToolBrush")
    if brush is not None:
        brush.click()
        yield 0.3
        check("switching tools deactivates", not tool.active and not btn.isChecked())
        check("prompt bar removed", tool.prompt is None)
        shot("deactivated")

    doc.setModified(False)


def finish():
    errors = [w.windowTitle() for w in QApplication.topLevelWidgets() if w.isVisible() and "error" in w.windowTitle().lower()]
    check("no script-error dialogs", not errors, errors)
    (OUT / "report.json").write_text(json.dumps(report, indent=2))
    for d in app.documents():
        d.setModified(False)
    if HOVER_WAS is not None:
        settings.put("hoverPreview", HOVER_WAS)
    QTimer.singleShot(300, lambda: app.action("file_quit").trigger())


def drive(gen, deadline=None):
    try:
        step = next(gen)
    except StopIteration:
        finish()
        return
    except Exception:  # noqa: BLE001
        report["error"] = traceback.format_exc()
        finish()
        return
    if callable(step):
        limit = deadline or time.time() + 120
        if step() or time.time() > limit:
            if time.time() > limit:
                report["notes"].append("condition timed out")
            QTimer.singleShot(0, lambda: drive(gen))
        else:
            # re-evaluate the same condition later
            QTimer.singleShot(50, lambda: _poll(gen, step, limit))
    else:
        QTimer.singleShot(int(step * 1000), lambda: drive(gen))


def _poll(gen, cond, limit):
    if cond() or time.time() > limit:
        if time.time() > limit:
            report["notes"].append("condition timed out")
        drive(gen)
    else:
        QTimer.singleShot(50, lambda: _poll(gen, cond, limit))


drive(steps())
