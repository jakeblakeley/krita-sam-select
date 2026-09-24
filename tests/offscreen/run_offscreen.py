"""Drive the plugin in a fake Krita window, offscreen, with the real backend.

Uses Krita's own PyQt5 build. Run via tests/offscreen/run.sh. Installs the
backend into ~/Library/Application Support/SamSelect if it isn't there yet.
"""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "src"))

from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt5.QtGui import QKeyEvent, QMouseEvent, QTabletEvent  # noqa: E402
from PyQt5.QtWidgets import QAction, QApplication, QPushButton, QToolButton  # noqa: E402

import fake_krita  # noqa: E402

fake_krita.install_module()
app = QApplication(sys.argv)
fake_krita.Krita.addExtension = lambda self, ext: None

from samselect import backend as bk  # noqa: E402
from samselect import modes, settings  # noqa: E402
from samselect.controller import SamSelectTool  # noqa: E402

OUT = Path(os.environ.get("SAMSELECT_TEST_OUT", "/tmp"))
failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def pump(seconds=0.0, until=None):
    end = time.time() + seconds
    while True:
        app.processEvents()
        if until is not None and until():
            return True
        if time.time() >= end:
            return until is None
        time.sleep(0.005)


def mouse(widget, kind, pos, button=Qt.LeftButton, buttons=None, mods=Qt.NoModifier):
    if buttons is None:
        buttons = button if kind != QEvent.MouseButtonRelease else Qt.NoButton
    ev = QMouseEvent(kind, QPointF(pos), widget.mapToGlobal(QPoint(*map(int, (pos.x(), pos.y())))), button, buttons, mods)
    return QApplication.sendEvent(widget, ev)


def pen(kind, pos, button, buttons, pressure):
    ev = QTabletEvent(kind, QPointF(pos), QPointF(pos), QTabletEvent.Stylus, QTabletEvent.Pen, pressure, 0, 0, 0.0, 0.0, 0, Qt.NoModifier, 1, button, buttons)
    return QApplication.sendEvent(CANVAS[0], ev)


CANVAS = []


def widget_pos(view, x, y):
    return QPointF(view.offset[0] + x * view.zoom, view.offset[1] + y * view.zoom)


def main() -> int:
    image = fake_krita.test_image()
    win, toolbox, canvas, tool_label = fake_krita.build_main_window(image)
    CANVAS.append(canvas)
    doc = fake_krita.Document(image)
    view = fake_krita.View(doc)
    window = fake_krita.Window(win, view)
    fake_krita.Krita.instance().window = win
    pump(0.1)

    print("toolbox integration")
    tool = SamSelectTool(window)
    pump(0.2)
    btn = tool.toolbox.button
    section = toolbox.findChild(QToolButton, "KisToolSelectContiguous").parentWidget()
    nav = next(w for w in toolbox.children() if w.objectName() == "navigation")
    check(btn is not None and btn.parentWidget() is section, "button lives in the Select section")
    check(btn.geometry().topLeft() == QPoint(0, 4 * 32), f"button in the new 5th row (got {btn.geometry()})")
    check(section.height() == 5 * 32, f"section grew by one row (h={section.height()})")
    check(nav.y() == section.y() + section.height() + 6, f"following section pushed down (y={nav.y()})")
    check(btn.isVisible(), "button visible")

    print("activation")
    krita_active = toolbox.findChild(QToolButton, "KritaShape/KisToolBrush")
    krita_active.setChecked(True)
    btn.click()
    pump(0.1)
    check(tool.active and btn.isChecked() and not krita_active.isChecked(), "clicking the button activates SAM Select")
    check(tool.prompt is not None and tool.prompt.isVisible(), "prompt bar shown on canvas")
    check(tool.prompt.edit.placeholderText() == "type what to select", "placeholder text")
    check(tool.options.isVisible() and not tool_label.isVisible(), "options replace the tool's widgets in Tool Options")
    check(canvas.cursor().pixmap().cacheKey() != 0, "custom cursor set")

    print("backend")
    backend = bk.Backend.instance()
    if not backend.is_installed():
        print("  installing backend (first run)…")
        backend.install()
        pump(1200, until=lambda: backend.state in (bk.READY, bk.ERROR))
    else:
        backend.ensure_started()
    ok = pump(600, until=lambda: backend.state in (bk.READY, bk.ERROR))
    check(ok and backend.state == bk.READY, f"backend ready ({backend.state}: {backend.message})")
    if backend.state != bk.READY:
        return 1
    pump(3, until=lambda: backend.has_image(tool._capture.key) if tool._capture else False)
    check(tool._capture is not None and backend.has_image(tool._capture.key), "image prefetched on activation")

    def wait_selection(n, timeout=20):
        return pump(timeout, until=lambda: len(doc.selections) >= n and tool._pending == 0)

    print("click (replace)")
    p = widget_pos(view, 350, 400)  # centre of the red disc
    mouse(canvas, QEvent.MouseButtonPress, p)
    mouse(canvas, QEvent.MouseButtonRelease, p)
    check(wait_selection(1), "click produced a selection")
    sel = doc.selections[-1]
    r = sel.rect
    check(abs(r.x() - 200) <= 6 and abs(r.y() - 250) <= 6 and abs(r.width() - 300) <= 12 and abs(r.height() - 300) <= 12, f"mask bbox matches the red disc ({r})")
    check(sel.bound and sel.ops.count(("invert",)) == 2, "fresh selection bound to image + outline refreshed")

    print("shift+click (add)")
    p = widget_pos(view, 870, 400)
    mouse(canvas, QEvent.MouseButtonPress, p, mods=Qt.ShiftModifier)
    mouse(canvas, QEvent.MouseButtonRelease, p, mods=Qt.ShiftModifier)
    check(wait_selection(2), "shift+click produced a selection")
    sel = doc.selections[-1]
    check(sel.ops[0] == ("duplicate",) and ("add", "Selection") in sel.ops, f"combined with add ({sel.ops[:3]})")

    print("option+lasso (subtract) around the blue disc")
    ring = [widget_pos(view, 870 + 170 * math.cos(a), 400 + 170 * math.sin(a)) for a in (i * 2 * math.pi / 40 for i in range(41))]
    mouse(canvas, QEvent.MouseButtonPress, ring[0], mods=Qt.AltModifier)
    for pt in ring[1:20]:
        mouse(canvas, QEvent.MouseMove, pt, buttons=Qt.LeftButton, mods=Qt.AltModifier)
    check(tool.overlay.lasso is not None and tool.overlay.lasso.count() > 10, "freehand lasso path drawn")
    for pt in ring[20:]:
        mouse(canvas, QEvent.MouseMove, pt, buttons=Qt.LeftButton, mods=Qt.AltModifier)
    mouse(canvas, QEvent.MouseButtonRelease, ring[-1], mods=Qt.AltModifier)
    check(wait_selection(3), "lasso produced a selection")
    check(("subtract", "Selection") in doc.selections[-1].ops, "combined with subtract")
    check(tool.overlay.lasso is None, "lasso path cleared")

    print("text prompt (shift+return = add)")
    tool.prompt.edit.setFocus()
    tool.prompt.edit.setText("circle")
    QApplication.sendEvent(tool.prompt.edit, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.ShiftModifier))
    check(wait_selection(4), "text prompt produced a selection")
    check(("add", "Selection") in doc.selections[-1].ops, "text + shift adds")
    check(tool.prompt.status.text().endswith("found"), f"status shows count ({tool.prompt.status.text()!r})")

    print("space passes through (pan)")
    n = len(doc.selections)
    QApplication.sendEvent(tool.prompt.edit, QKeyEvent(QEvent.KeyPress, Qt.Key_Space, Qt.NoModifier, " "))
    check(not tool._space, "space typed into the text box is not a pan")
    QApplication.sendEvent(canvas, QKeyEvent(QEvent.KeyPress, Qt.Key_Space, Qt.NoModifier))
    p = widget_pos(view, 350, 400)
    mouse(canvas, QEvent.MouseButtonPress, p)
    mouse(canvas, QEvent.MouseButtonRelease, p)
    QApplication.sendEvent(canvas, QKeyEvent(QEvent.KeyRelease, Qt.Key_Space, Qt.NoModifier))
    pump(0.5)
    check(len(doc.selections) == n, "space+click did not select")

    print("hover preview")
    mouse(canvas, QEvent.MouseMove, widget_pos(view, 350, 400), button=Qt.NoButton, buttons=Qt.NoButton)
    check(pump(5, until=lambda: tool.overlay.preview is not None), "hover shows a preview")
    win.grab().save(str(OUT / "offscreen_hover.png"))

    print("stroke robustness (lost releases can't leave the lasso stuck)")
    ring = [widget_pos(view, 350 + 190 * math.cos(a), 400 + 190 * math.sin(a)) for a in (i * 2 * math.pi / 30 for i in range(31))]

    def lasso_moves(points, tablet=False, buttons=Qt.LeftButton, pressure=0.6):
        for pt in points:
            if tablet:
                pen(QEvent.TabletMove, pt, Qt.NoButton, buttons, pressure)
            else:
                mouse(canvas, QEvent.MouseMove, pt, buttons=buttons)

    n = len(doc.selections)
    mouse(canvas, QEvent.MouseButtonPress, ring[0])
    lasso_moves(ring[1:])
    check(tool._press is not None and tool._press["drag"], "mouse lasso in progress")
    mouse(canvas, QEvent.MouseMove, ring[-1], button=Qt.NoButton, buttons=Qt.NoButton)  # release was lost
    check(tool._press is None and tool.overlay.lasso is None, "a move with the button up ends the stroke")
    check(wait_selection(n + 1), "...and commits the lasso")
    mouse(canvas, QEvent.MouseMove, widget_pos(view, 100, 100), button=Qt.NoButton, buttons=Qt.NoButton)
    check(tool._press is None and tool.overlay.lasso is None, "later hover moves don't draw")

    n = len(doc.selections)
    pen(QEvent.TabletPress, ring[0], Qt.LeftButton, Qt.LeftButton, 0.6)
    lasso_moves(ring[1:], tablet=True)
    pen(QEvent.TabletRelease, ring[-1], Qt.NoButton, Qt.NoButton, 0.0)  # driver reports no button on pen-up
    check(tool._press is None, "tablet release with button()=NoButton ends the stroke")
    check(wait_selection(n + 1), "...and commits the lasso")

    n = len(doc.selections)
    pen(QEvent.TabletPress, ring[0], Qt.LeftButton, Qt.LeftButton, 0.6)
    lasso_moves(ring[1:], tablet=True)
    pen(QEvent.TabletMove, ring[-1], Qt.NoButton, Qt.NoButton, 0.0)  # lifted, release never came
    check(tool._press is None, "tablet move with no pressure/buttons ends the stroke")
    check(wait_selection(n + 1), "...and commits the lasso")

    n = len(doc.selections)
    pen(QEvent.TabletPress, ring[0], Qt.LeftButton, Qt.LeftButton, 0.6)
    lasso_moves(ring[1:], tablet=True, buttons=Qt.NoButton, pressure=0.5)
    check(tool._press is not None, "pressure keeps a stroke alive when a driver drops the button state")
    QApplication.sendEvent(app, QTabletEvent(QEvent.TabletLeaveProximity, QPointF(), QPointF(), QTabletEvent.Stylus, QTabletEvent.Pen, 0.0, 0, 0, 0.0, 0.0, 0, Qt.NoModifier, 1, Qt.NoButton, Qt.NoButton))
    check(tool._press is None, "pen leaving proximity ends the stroke")
    check(wait_selection(n + 1), "...and commits the lasso")

    n = len(doc.selections)
    mouse(canvas, QEvent.MouseButtonPress, ring[0])
    lasso_moves(ring[1:15])
    app.applicationStateChanged.emit(Qt.ApplicationInactive)
    check(tool._press is None and tool.overlay.lasso is None, "switching apps mid-stroke cancels it")
    app.applicationStateChanged.emit(Qt.ApplicationActive)
    pump(0.5)
    check(len(doc.selections) == n, "...without selecting")

    n = len(doc.selections)
    mouse(canvas, QEvent.MouseButtonPress, ring[0])
    lasso_moves(ring[1:15])
    mouse(canvas, QEvent.MouseButtonPress, widget_pos(view, 350, 400))  # stale stroke, new press
    check(tool._press is not None and not tool._press["drag"], "a new press replaces a stale stroke")
    mouse(canvas, QEvent.MouseButtonRelease, widget_pos(view, 350, 400))
    check(wait_selection(n + 1) and len(doc.selections) == n + 1, "...and the click selects once")

    mouse(canvas, QEvent.MouseButtonPress, ring[0])
    lasso_moves(ring[1:10])
    mouse(canvas, QEvent.MouseButtonRelease, ring[9], button=Qt.RightButton, buttons=Qt.LeftButton)
    check(tool._press is not None, "releasing another button mid-stroke keeps the lasso")
    QApplication.sendEvent(canvas, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    check(tool._press is None and tool.overlay.lasso is None, "Escape cancels")

    print("Krita's selection actions bar takes priority")
    decoy = QPushButton(canvas)  # an unrelated canvas child: must not count as the bar
    decoy.setGeometry(600, 600, 30, 30)
    decoy.show()
    bar = fake_krita.add_actions_bar(canvas, 180, 150)
    pump(0.05)  # no canvas repaint: showing the bar's widgets must be enough
    expect = bar[0].geometry().united(bar[-1].geometry()).adjusted(-6, -6, 6, 6)
    check(tool.overlay.exclude == expect, f"overlay finds the bar by Krita's markers, not class names ({tool.overlay.exclude})")

    def deliver(kind, pt, mods=Qt.NoModifier):
        # Route like Qt: the widget under the pointer gets it (transparent overlays skipped).
        target = canvas.childAt(pt.toPoint()) or canvas
        local = QPointF(target.mapFrom(canvas, pt.toPoint()))
        return target, mouse(target, kind, local, mods=mods)

    clicked = []
    bar[2].clicked.connect(lambda: clicked.append(True))
    n = len(doc.selections)
    on_button = QPointF(bar[2].geometry().center())
    target, _ = deliver(QEvent.MouseButtonPress, on_button)
    deliver(QEvent.MouseButtonRelease, on_button)
    check(target is bar[2], f"clicks over a bar button reach the button, not the overlay ({type(target).__name__})")
    check(clicked == [True], "the bar button fires")
    in_margin = QPointF(bar[0].geometry().left() + 40, bar[0].geometry().top() - 3)  # painted outline, no widget
    target, _ = deliver(QEvent.MouseButtonPress, in_margin)
    check(target is canvas and tool._press is None, "a press on the bar's painted margin doesn't start a SAM stroke")
    deliver(QEvent.MouseButtonRelease, in_margin)
    pump(0.5)
    check(len(doc.selections) == n, "...and selects nothing")

    tool.overlay.set_preview(bytes([255]) * (100 * 100), 0, 0, 100, 100, 100 / max(doc.width(), doc.height()))
    shot = tool.overlay.grab().toImage()
    inside = expect.center()
    outside = QPoint(expect.right() + 40, expect.bottom() + 40)
    check(shot.pixelColor(inside).alpha() == 0, "hover preview is not painted over the bar")
    check(shot.pixelColor(outside).alpha() > 0, "...but is painted everywhere else")
    mouse(canvas, QEvent.MouseMove, in_margin, button=Qt.NoButton, buttons=Qt.NoButton)
    check(tool.overlay.preview is None, "hovering the bar clears the preview")

    for w in bar:  # drag the bar onto the text box (no canvas repaint)
        w.move(w.x() - 180 + (canvas.width() - 8 * 30) // 2, canvas.height() - 40)
    pump(0.05)
    moved = bar[0].geometry().united(bar[-1].geometry()).adjusted(-6, -6, 6, 6)
    check(tool.overlay.exclude == moved, "the hole follows the bar when it moves")
    check(not tool.prompt.geometry().intersects(tool.overlay.exclude), f"text box moves clear of the bar ({tool.prompt.geometry()} vs {tool.overlay.exclude})")
    for w in bar:
        w.hide()
    pump(0.05)
    check(tool.overlay.exclude is None and tool.prompt.geometry().bottom() > canvas.height() - 40, "bar hidden: overlay and text box return to normal")

    print("mode action + cursor")
    win.findChild(QAction, "selection_tool_mode_add").trigger()
    check(settings.mode() == modes.ADD, "selection_tool_mode_add switches the (shared) mode")
    settings.set_mode(modes.REPLACE)

    print("deactivation")
    krita_active.click()
    pump(0.1)
    check(not tool.active and not btn.isChecked(), "clicking a Krita tool deactivates SAM Select")
    check(tool_label.isVisible() and not tool.options.isVisible(), "Tool Options restored")
    check(tool.prompt is None, "prompt bar removed")

    win.grab().save(str(OUT / "offscreen_after.png"))
    backend.shutdown()
    print(f"\n{len(failures)} failure(s)")
    for f in failures:
        print("  -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
