"""SAM Select tool: activation, canvas input, and prompt -> selection flow.

Krita does not let Python register tools, so SAM Select behaves like one:

* Activating it switches Krita to the Contiguous Selection tool underneath
  (so Krita's selection-mode actions and modifier handling are live), then
  checks our toolbox button, which unchecks Krita's.
* While active, an application-level event filter sees canvas input before
  Krita's input manager (which re-installs its own canvas filter on every
  focus change). Left-button clicks, freehand lasso drags and tablet
  strokes become SAM prompts; everything else - wheel, pinch, middle/right buttons,
  Space+drag panning, shortcuts - passes straight through to Krita.
* Our cursor, overlay and text box live on the canvas widget; options live
  in the Tool Options docker.
"""

from __future__ import annotations

import time
import traceback
from pathlib import Path

from krita import Krita
from PyQt5.QtCore import QEvent, QObject, QPointF, Qt, QTimer
from PyQt5.QtGui import QColor, QIcon
from PyQt5.QtWidgets import QAbstractSpinBox, QAction, QApplication, QLineEdit, QTextEdit

from . import backend as bk
from . import canvas as cv
from . import cursors, imaging, modes, selection_ops, settings
from .options import ToolOptionsHost, ToolOptionsWidget
from .overlay import CanvasOverlay, PromptBar
from .toolbox import ToolboxButton

BASE_TOOL = "KisToolSelectContiguous"
TOOL_NAME = "SAM Select"
ICON_DIR = Path(__file__).resolve().parent / "icons"
DRAG_THRESHOLD = 4  # logical px (mouse)
DRAG_THRESHOLD_TABLET = 7
LASSO_STEP = 2.0  # logical px between recorded lasso points
LASSO_MAX_POINTS = 256  # sent to the backend
HOVER_DELAY_MS = 30
PREVIEW_MAX_SIDE = 1024

PRESS = {QEvent.MouseButtonPress, QEvent.MouseButtonDblClick, QEvent.TabletPress}
MOVE = {QEvent.MouseMove, QEvent.TabletMove}
RELEASE = {QEvent.MouseButtonRelease, QEvent.TabletRelease}
POINTER = PRESS | MOVE | RELEASE
TABLET = {QEvent.TabletPress, QEvent.TabletMove, QEvent.TabletRelease}
KEYS = {QEvent.KeyPress, QEvent.KeyRelease}
GEOMETRY = {QEvent.Show, QEvent.Hide, QEvent.Move, QEvent.Resize}
MODIFIER_KEYS = {Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta}


def tool_icon() -> QIcon:
    dark = QApplication.palette().window().color().lightness() < 128
    return QIcon(str(ICON_DIR / ("samselect_dark.svg" if dark else "samselect_light.svg")))


def _overlay_color() -> QColor:
    """Krita's configured selection overlay colour (Preferences > Display)."""
    raw = Krita.instance().readSetting("", "selectionOverlayMaskColor", "")
    try:
        r, g, b, *_ = (int(x) for x in raw.split(","))
        return QColor(r, g, b)
    except ValueError:
        return QColor(255, 60, 110)


TEXT_INPUTS = (QLineEdit, QTextEdit, QAbstractSpinBox)


class SamSelectTool(QObject):
    def __init__(self, window) -> None:
        super().__init__(window.qwindow())
        self.window = window
        self.qwindow = window.qwindow()
        self.backend = bk.Backend.instance()
        self.active = False
        self._guard = False
        self.view = None
        self.canvas = None
        self.overlay: CanvasOverlay | None = None
        self.prompt: PromptBar | None = None
        self._press = None  # dict while the left button is down
        self._space = False
        self._pending = 0
        self._capture: imaging.Capture | None = None
        self._hover_uv = None
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.timeout.connect(self._send_hover)
        self._cursor_timer = QTimer(self)
        self._cursor_timer.setSingleShot(True)
        self._cursor_timer.timeout.connect(self._apply_cursor)
        self._bar_timer = QTimer(self)
        self._bar_timer.setSingleShot(True)
        self._bar_timer.timeout.connect(self._sync_bar)
        self._stroke_watchdog = QTimer(self)
        self._stroke_watchdog.setInterval(120)
        self._stroke_watchdog.timeout.connect(self._check_stroke)
        QApplication.instance().applicationStateChanged.connect(self._on_app_state)

        self.options = ToolOptionsWidget()
        self.options.modeChanged.connect(lambda _m: self._apply_cursor())
        self.options.installRequested.connect(self.backend.install)
        self.options_host = ToolOptionsHost(self.qwindow)

        shortcut = self._shortcut_text()
        tip = f"{TOOL_NAME}{f' ({shortcut})' if shortcut else ''}\nSelect objects with SAM 3: click, lasso around objects, or type a description."
        self.toolbox = ToolboxButton(self.qwindow, tool_icon(), tip)
        self.toolbox.toggled.connect(self._on_toggled)

        base = self._action(BASE_TOOL)
        if base is not None:
            base.triggered.connect(self._on_base_tool_triggered)
        for name, mode in modes.MODE_ACTIONS.items():
            act = self._action(name)
            if act is not None:
                act.triggered.connect(lambda _=False, m=mode: self._on_mode_action(m))
        window.activeViewChanged.connect(self._on_view_changed)
        window.themeChanged.connect(lambda: self.toolbox.set_icon(tool_icon()))
        window.windowClosed.connect(self.shutdown)
        self.backend.stateChanged.connect(self._on_backend_state)

    # --------------------------------------------------------------- helpers

    def _action(self, name: str) -> QAction | None:
        return self.qwindow.findChild(QAction, name) or Krita.instance().action(name)

    def _shortcut_text(self) -> str:
        act = self._action("samselect_tool")
        return act.shortcut().toString() if act is not None and not act.shortcut().isEmpty() else ""

    def _message(self, text: str, ms: int = 2500) -> None:
        if self.view is not None:
            self.view.showFloatingMessage(text, tool_icon(), ms, 1)

    def _current_mode(self, mods: int | None = None) -> str:
        if mods is None:
            mods = int(QApplication.keyboardModifiers())
        return modes.mode_for_modifiers(mods, settings.mode(), settings.swap_ctrl_alt())

    # ------------------------------------------------------------ activation

    def activate(self) -> None:
        """Entry point for the toolbox button, shortcut and menu action."""
        if self.active:
            return
        if self.toolbox.ok:
            self.toolbox.set_checked(True)  # -> _on_toggled(True)
        else:
            self._activate()

    def _on_toggled(self, checked: bool) -> None:
        if self._guard:
            return
        if checked:
            self._activate()
        else:
            self._deactivate()

    def _activate(self) -> None:
        self._guard = True
        try:
            base = self._action(BASE_TOOL)
            if base is not None:
                base.trigger()  # Krita-side active tool = Contiguous Selection
            if self.toolbox.ok:
                self.toolbox.set_checked(True)
        finally:
            self._guard = False
        self.active = True
        self.backend.idle_minutes = settings.get("idleUnloadMinutes")
        self._attach()
        if self.backend.state == bk.MISSING:
            self._message("SAM Select needs a one-time setup — see Tool Options.", 4000)
        else:
            self.backend.ensure_started()

    def _deactivate(self) -> None:
        if not self.active:
            return
        self.active = False
        self._detach()
        # Clicking the (already active) Contiguous Selection button doesn't
        # make Krita re-apply its cursor; do it for the tool we're leaving to.
        QTimer.singleShot(0, self._restore_krita_cursor_if_needed)

    def _restore_krita_cursor_if_needed(self) -> None:
        group = self.toolbox.group
        checked = group.checkedButton() if group is not None else None
        if checked is not None and checked.objectName() == BASE_TOOL:
            from .toolbox import set_highlight

            set_highlight(checked)
            canvas = cv.canvas_widget(self.qwindow)
            if canvas is not None:
                from PyQt5.QtGui import QCursor, QPixmap

                pm = QPixmap(":/tool_contiguous_selection_cursor.png")
                if not pm.isNull():
                    canvas.setCursor(QCursor(pm, 6, 6))

    def _on_base_tool_triggered(self) -> None:
        # The Contiguous Selection shortcut while we're active: Krita sees no
        # tool change (it is already the active tool), so leave explicitly.
        if self.active and not self._guard:
            self.toolbox.check_krita_button(BASE_TOOL)

    def _on_mode_action(self, mode: str) -> None:
        if self.active:
            settings.set_mode(mode)
            self.options.set_mode(mode)
            self._apply_cursor()

    def _on_view_changed(self) -> None:
        if self.active:
            self._detach()
            self._attach()

    def _on_backend_state(self, state: str, message: str, _progress: float) -> None:
        if not self.active:
            return
        if state == bk.READY:
            self._prefetch()
        elif state == bk.ERROR and message:
            self._message(f"SAM Select: {message.splitlines()[0]}", 5000)

    # ------------------------------------------------------------- attach

    def _attach(self) -> None:
        self.view = self.window.activeView()
        self.canvas = cv.canvas_widget(self.qwindow)
        if not self.options_host.show(self.options):
            pass  # Tool Options shown in the toolbar popup, or the docker is closed
        self.options.reload()
        if self.view is None or self.canvas is None:
            return
        self.overlay = CanvasOverlay(self.canvas)
        self.overlay.color = _overlay_color()
        self.prompt = PromptBar(self.canvas)
        self.prompt.submitted.connect(self._on_text)
        self.canvas.destroyed.connect(self._on_canvas_destroyed)
        QApplication.instance().installEventFilter(self)
        self._apply_cursor()
        self._prefetch()

    def _detach(self) -> None:
        QApplication.instance().removeEventFilter(self)
        self._hover_timer.stop()
        self.backend.drop("hover")
        self._cancel_stroke()
        if self.overlay is not None:
            self.overlay.detach()
        if self.prompt is not None:
            self.prompt.detach()
        self.overlay = self.prompt = None
        if self.canvas is not None:
            try:
                self.canvas.destroyed.disconnect(self._on_canvas_destroyed)
            except TypeError:
                pass
        self.canvas = None
        self.view = None
        self.options_host.hide()

    def _on_canvas_destroyed(self, *_args) -> None:
        self.canvas = None
        self.overlay = self.prompt = None
        self._cancel_stroke()
        QApplication.instance().removeEventFilter(self)

    def shutdown(self) -> None:
        self._deactivate()
        self.toolbox.remove()

    # ------------------------------------------------------------- cursor

    def _apply_cursor(self) -> None:
        if self.active and self.canvas is not None:
            cur = cursors.cursor(self._current_mode())
            if self.canvas.cursor().pixmap().cacheKey() != cur.pixmap().cacheKey():
                self.canvas.setCursor(cur)

    # -------------------------------------------------------------- events

    def eventFilter(self, obj, event) -> bool:
        t = event.type()
        if t in KEYS:
            return self._on_key(obj, event, t)
        if t == QEvent.TabletLeaveProximity:  # sent to the application, not the canvas
            if self._press is not None and self._press["tablet"]:
                self._end_stroke()
            return False
        if obj is not self.canvas:
            if t == QEvent.Enter and cv.is_actions_bar_widget(obj, self.canvas):
                self._clear_hover()  # pointer moved onto Krita's selection actions bar
            elif t in GEOMETRY and self.overlay is not None and cv.is_actions_bar_widget(obj, self.canvas):
                self._bar_timer.start(0)  # the bar appeared, moved or hid
            return False
        if t in POINTER:
            try:
                return self._on_pointer(event, t)
            except Exception:  # noqa: BLE001 - never leave a stroke half-open
                self._cancel_stroke()
                traceback.print_exc()
                return False
        if t == QEvent.Enter:
            self._apply_cursor()
        elif t == QEvent.Leave:
            self._clear_hover()
        elif t == QEvent.FocusOut and self._press is not None and event.reason() != Qt.PopupFocusReason:
            self._cancel_stroke()  # a dialog or another window took over mid-stroke
        elif t == QEvent.CursorChange:
            # Krita's tools re-set their cursor (e.g. 100 ms after a modifier
            # key); put ours back once the dust settles.
            self._cursor_timer.start(0)
        elif t == QEvent.Paint and self.overlay is not None:
            self._sync_overlay()
        return False

    def _on_key(self, receiver, event, t) -> bool:
        key = event.key()
        if key == Qt.Key_Space and not event.isAutoRepeat() and not isinstance(receiver, TEXT_INPUTS):
            self._space = t == QEvent.KeyPress
        elif key in MODIFIER_KEYS:
            self._cursor_timer.start(0)
        elif key == Qt.Key_Escape and t == QEvent.KeyPress and self._press is not None:
            self._cancel_stroke()
            return True
        return False

    def _sync_overlay(self) -> None:
        tf = cv.image_to_widget(self.view)
        if tf != self.overlay.image_to_widget:
            self.overlay.image_to_widget = tf
            self.overlay.update()
        self._sync_bar()

    def _sync_bar(self) -> None:
        """Krita's selection actions bar is painted by the canvas, i.e. under our
        overlay: repaint around it when it changes and keep the text box clear."""
        if self.overlay is None:
            return
        bar = cv.actions_bar_rect(self.canvas)
        if bar != self.overlay.exclude:
            self.overlay.exclude = bar
            self.overlay.update()
        if self.prompt is not None:
            self.prompt.avoid(bar)

    def _on_actions_bar(self, pos: QPointF) -> bool:
        bar = cv.actions_bar_rect(self.canvas)
        return bar is not None and bar.contains(pos.toPoint())

    @staticmethod
    def _left_held(event, tablet: bool) -> bool:
        if event.buttons() & Qt.LeftButton:
            return True
        # Some tablet drivers report no buttons mid-stroke; pressure still tells.
        return tablet and event.pressure() > 0.0

    def _on_pointer(self, event, t) -> bool:
        tablet = t in TABLET
        pos = QPointF(event.posF() if tablet else event.localPos())
        if t in PRESS:
            if event.button() != Qt.LeftButton or self._space:
                return False
            if self._on_actions_bar(pos):
                # Krita's selection actions bar has priority: its buttons get
                # their own events; its painted margin and gaps are dead space.
                event.accept()
                return True
            self._begin_stroke(event, pos, tablet)
            event.accept()
            return True
        if t in MOVE:
            if self._press is None:
                if not (event.buttons() & Qt.LeftButton):
                    self._hover_at(pos)
                return False
            if not self._left_held(event, tablet):
                # We never saw the release (lost to another window, a driver
                # quirk...): the button is up now, so finish where it went up.
                self._end_stroke(pos)
                return False
            self._update_stroke(pos)
            event.accept()
            return True
        if t in RELEASE:
            if self._press is None:
                return False
            # Tablet drivers may report NoButton on pen-up; what matters is
            # that the left button (tip) is no longer held.
            if event.button() != Qt.LeftButton and self._left_held(event, tablet):
                return False  # another button went up mid-stroke; Krita owns it
            self._end_stroke(pos)
            event.accept()
            return True
        return False

    # -------------------------------------------------------------- strokes
    #
    # A stroke is one left-button press: it becomes a click or a freehand
    # lasso. It always ends through _end_stroke (commit) or _cancel_stroke,
    # and several independent signals can end it, so a single missed release
    # can never leave the tool drawing a lasso with no button held:
    #   release event · a move with the button up · tablet leaving proximity ·
    #   Escape · focus lost / app deactivated (cancel) · a watchdog that polls
    #   Qt's button state for mouse strokes · detaching from the canvas.

    def _begin_stroke(self, event, pos: QPointF, tablet: bool) -> None:
        if self._press is not None:
            self._cancel_stroke()  # a stale stroke whose release never arrived
        self._clear_hover()
        self.canvas.setFocus(Qt.MouseFocusReason)
        img = cv.to_image(self.view, pos)
        self._press = {
            "pos": pos,
            "img": img,
            "mode": self._current_mode(int(event.modifiers())),
            "tablet": tablet,
            "drag": False,
            "path": [img],  # freehand lasso, image coordinates
            "last": pos,  # last recorded lasso point
            "cursor": pos,  # latest pointer position
        }
        # Qt's global button state only tracks real (spontaneous) mouse input,
        # not tablet strokes, so the watchdog is for mouse strokes only.
        if not tablet and event.spontaneous():
            self._stroke_watchdog.start()

    def _update_stroke(self, pos: QPointF) -> None:
        press = self._press
        press["cursor"] = pos
        limit = DRAG_THRESHOLD_TABLET if press["tablet"] else DRAG_THRESHOLD
        if not press["drag"] and (pos - press["pos"]).manhattanLength() >= limit:
            press["drag"] = True
        if press["drag"] and (pos - press["last"]).manhattanLength() >= LASSO_STEP:
            press["path"].append(cv.to_image(self.view, pos))
            press["last"] = pos
            if self.overlay is not None:
                self._sync_overlay()
                self.overlay.set_lasso(press["path"])

    def _end_stroke(self, pos: QPointF | None = None) -> None:
        """Commit the stroke: a click, or a lasso if it moved past the threshold."""
        press, self._press = self._press, None
        self._stroke_watchdog.stop()
        if self.overlay is not None:
            self.overlay.set_lasso(None)
        self._cursor_timer.start(0)
        if press is None or self.view is None:
            return
        pos = press["cursor"] if pos is None else pos
        if press["drag"]:
            press["path"].append(cv.to_image(self.view, pos))
            self._finish_lasso(press, pos)
        else:
            self._finish_click(press, pos)

    def _cancel_stroke(self) -> None:
        self._press = None
        self._stroke_watchdog.stop()
        if self.overlay is not None:
            self.overlay.set_lasso(None)

    def _check_stroke(self) -> None:
        if self._press is not None and not self._press["tablet"] and not (QApplication.mouseButtons() & Qt.LeftButton):
            self._end_stroke()

    def _on_app_state(self, state) -> None:
        if state != Qt.ApplicationActive and self._press is not None:
            self._cancel_stroke()

    # ------------------------------------------------------------- prompts

    def _doc_size(self):
        doc = self.view.document() if self.view is not None else None
        return (doc, doc.width(), doc.height()) if doc is not None else (None, 0, 0)

    def _finish_click(self, press, pos) -> None:
        doc, w, h = self._doc_size()
        if doc is None:
            return
        u, v = press["img"].x() / w, press["img"].y() / h
        if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
            return
        self._run({"type": "point", "points": [[u, v, 1]]}, press["mode"], pos, "click")

    def _finish_lasso(self, press, pos) -> None:
        doc, w, h = self._doc_size()
        if doc is None:
            return
        path = press["path"]
        # Shoelace area in image pixels; a scribble that encloses almost
        # nothing is treated as a click where it started.
        area = 0.5 * abs(sum(a.x() * b.y() - b.x() * a.y() for a, b in zip(path, path[1:] + path[:1])))
        if len(path) < 3 or area < 64:
            self._finish_click(press, press["pos"])
            return
        step = max(1, -(-len(path) // LASSO_MAX_POINTS))
        points = [[min(max(p.x() / w, 0.0), 1.0), min(max(p.y() / h, 0.0), 1.0)] for p in path[::step]]
        prompt = {"type": "lasso", "points": points, "objects": bool(settings.get("lassoSelectsAllObjects"))}
        self._run(prompt, press["mode"], pos, "lasso")

    def _on_text(self, text: str, mods: int) -> None:
        if not self.active:
            return
        prompt = {"type": "text", "text": text, "threshold": float(settings.get("textThreshold"))}
        anchor = QPointF(self.prompt.geometry().center().x(), self.prompt.geometry().top() - 36)
        self.prompt.show_status("…")
        self._run(prompt, self._current_mode(mods), anchor, "text")
        self.prompt.edit.selectAll()

    def _prefetch(self) -> None:
        """Upload the current image so SAM encodes it before the first click."""
        if not self.active or self.view is None or not self.backend.is_installed():
            return
        cap = imaging.capture(self.view.document(), self._reference())
        if cap is not None:
            self._capture = cap
            self.backend.send_image(cap.key, cap.rgb)

    def _reference(self) -> str:
        return "layer" if settings.get("sampleLayersMode") == settings.SAMPLE_CURRENT else "image"

    def _run(self, prompt: dict, mode: str, anchor: QPointF, kind: str) -> None:
        if self.backend.state == bk.MISSING:
            self._message("SAM Select needs a one-time setup — see Tool Options.", 4000)
            if self.prompt is not None:
                self.prompt.show_status("")
            return
        doc = self.view.document()
        cap = imaging.capture(doc, self._reference())
        if cap is None:
            return
        self._capture = cap
        self.backend.send_image(cap.key, cap.rgb)
        header = {
            "cmd": "segment",
            "key": cap.key,
            "prompt": prompt,
            "out": {"w": cap.width, "h": cap.height, "antialias": bool(settings.get("antiAliasSelection"))},
        }
        job = {"doc": doc, "cap": cap, "header": header, "mode": mode, "kind": kind, "retried": False, "t0": time.perf_counter()}
        self._submit(job, anchor)
        if self.backend.state != bk.READY:
            self._message(self.backend.message or "Starting SAM 3…")

    def _submit(self, job: dict, anchor: QPointF | None = None) -> None:
        self._pending += 1
        if self.overlay is not None:
            self.overlay.set_busy(True, anchor)
        self.backend.request(job["header"], callback=lambda h, d: self._on_result(job, h, d))

    def _on_result(self, job: dict, header: dict, data: bytes) -> None:
        self._pending = max(0, self._pending - 1)
        if self.overlay is not None and self._pending == 0:
            self.overlay.set_busy(False)
        kind, prompt = job["kind"], job["header"]["prompt"]
        if not header.get("ok"):
            if header.get("error") == "unknown_image" and not job["retried"]:
                job["retried"] = True  # the server restarted or evicted the image
                self.backend.send_image(job["cap"].key, job["cap"].rgb)
                self._submit(job)
                return
            self._message(f"SAM Select: {header.get('error', 'failed')}", 4000)
            if kind == "text" and self.prompt is not None:
                self.prompt.show_status("")
            return
        doc = job["doc"]
        try:
            same_size = doc.width() == job["cap"].width and doc.height() == job["cap"].height
        except RuntimeError:  # document closed meanwhile
            return
        if kind == "text" and self.prompt is not None:
            n = header.get("count", 0)
            self.prompt.show_status(f"{n} found" if n else "none found")
        if header.get("empty") or not same_size:
            if kind == "text":
                self._message(f"Nothing matching “{prompt['text']}” found.")
            elif kind == "lasso":
                self._message("No objects found inside the lasso.")
            return
        changed = selection_ops.apply_mask(
            doc,
            header["x"],
            header["y"],
            header["w"],
            header["h"],
            data,
            job["mode"],
            grow=int(settings.get("growSelection")),
            feather=int(settings.get("featherSelection")),
        )
        if not changed and job["mode"] == modes.SUBTRACT:
            self._message("Nothing to subtract from: there is no selection.")

    # --------------------------------------------------------------- hover

    def _hover_at(self, pos: QPointF) -> None:
        if not settings.get("hoverPreview") or self.backend.state != bk.READY or self._capture is None:
            return
        if self._on_actions_bar(pos):
            self._clear_hover()
            return
        doc, w, h = self._doc_size()
        if doc is None:
            return
        img = cv.to_image(self.view, pos)
        u, v = img.x() / w, img.y() / h
        if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
            self._clear_hover()
            return
        self._hover_uv = (u, v)
        self._hover_timer.start(HOVER_DELAY_MS)

    def _send_hover(self) -> None:
        cap = self._capture
        if not self.active or cap is None or self._hover_uv is None or self._press is not None or self._pending:
            return
        scale = min(1.0, PREVIEW_MAX_SIDE / max(cap.width, cap.height))
        header = {
            "cmd": "segment",
            "key": cap.key,
            "prompt": {"type": "point", "points": [[*self._hover_uv, 1]]},
            "out": {"w": max(1, round(cap.width * scale)), "h": max(1, round(cap.height * scale)), "antialias": True},
        }
        self.backend.send_image(cap.key, cap.rgb)
        self.backend.request(header, callback=lambda h, d: self._on_hover(h, d, scale), coalesce="hover")

    def _on_hover(self, header: dict, data: bytes, scale: float) -> None:
        if self.overlay is None or self._press is not None or self._hover_uv is None:
            return
        if not header.get("ok") or header.get("empty"):
            self.overlay.set_preview(None)
            return
        self._sync_overlay()
        self.overlay.set_preview(data, header["x"], header["y"], header["w"], header["h"], scale)

    def _clear_hover(self) -> None:
        self._hover_uv = None
        self._hover_timer.stop()
        self.backend.drop("hover")
        if self.overlay is not None:
            self.overlay.set_preview(None)
