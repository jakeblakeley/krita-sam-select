"""Krita extension: one SAM Select tool per main window, plus menu actions."""

from __future__ import annotations

import os

from krita import Extension, Krita
from PyQt5 import sip
from PyQt5.QtCore import QTimer

from . import backend as bk


def _key(qwindow) -> int:
    return sip.unwrapinstance(qwindow)


class SamSelectExtension(Extension):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.tools: dict[int, object] = {}
        self._selftest_done = False

    def setup(self) -> None:
        notifier = Krita.instance().notifier()
        notifier.setActive(True)
        notifier.windowCreated.connect(self._sync_windows)
        notifier.applicationClosing.connect(bk.Backend.instance().shutdown)

    def createActions(self, window) -> None:
        # `window` is only valid during this call (Krita deletes the wrapper
        # afterwards), so never capture it: resolve the window at trigger time.
        tool_action = window.createAction("samselect_tool", "SAM Select Tool", "tools/scripts")
        tool_action.triggered.connect(lambda *_: self._activate_in_active_window())
        setup_action = window.createAction("samselect_install", "SAM Select: Install or Repair Backend…", "tools/scripts")
        setup_action.triggered.connect(lambda *_: bk.Backend.instance().install())
        # The toolbox and dockers exist once the window has finished building.
        QTimer.singleShot(0, self._sync_windows)

    def _activate_in_active_window(self) -> None:
        window = Krita.instance().activeWindow()
        if window is not None:
            self._tool(window).activate()

    def _sync_windows(self) -> None:
        for window in Krita.instance().windows():
            self._tool(window)

    def _tool(self, window):
        from .controller import SamSelectTool

        qwindow = window.qwindow()
        key = _key(qwindow)
        tool = self.tools.get(key)
        if tool is None:
            tool = SamSelectTool(window)  # keeps this (Python-owned) Window wrapper alive
            self.tools[key] = tool
            qwindow.destroyed.connect(lambda *_: self.tools.pop(key, None))
            script = os.environ.get("SAMSELECT_SELFTEST")
            if script and not self._selftest_done:
                self._selftest_done = True
                QTimer.singleShot(1500, lambda: _run_selftest(script, tool))
        return tool


def _run_selftest(path: str, tool) -> None:
    """Developer hook: SAMSELECT_SELFTEST=/path/script.py runs a script inside Krita."""
    namespace = {"tool": tool, "Krita": Krita, "__file__": path}
    with open(path, encoding="utf-8") as f:
        code = compile(f.read(), path, "exec")
    exec(code, namespace)  # noqa: S102 - opt-in developer hook
