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

    def setup(self) -> None:
        notifier = Krita.instance().notifier()
        notifier.setActive(True)
        notifier.windowCreated.connect(self._on_window_created)
        notifier.applicationClosing.connect(bk.Backend.instance().shutdown)

    def createActions(self, window) -> None:
        tool_action = window.createAction("samselect_tool", "SAM Select Tool", "tools/scripts")
        tool_action.triggered.connect(lambda: self._tool(window).activate())
        setup_action = window.createAction("samselect_install", "SAM Select: Install or Repair Backend…", "tools/scripts")
        setup_action.triggered.connect(bk.Backend.instance().install)
        # The toolbox and dockers exist once the window has finished building.
        QTimer.singleShot(0, lambda: self._tool(window))

    def _on_window_created(self) -> None:
        for window in Krita.instance().windows():
            self._tool(window)

    def _tool(self, window):
        from .controller import SamSelectTool

        qwindow = window.qwindow()
        key = _key(qwindow)
        tool = self.tools.get(key)
        if tool is None:
            tool = SamSelectTool(window)
            self.tools[key] = tool
            qwindow.destroyed.connect(lambda *_: self.tools.pop(key, None))
            script = os.environ.get("SAMSELECT_SELFTEST")
            if script:
                QTimer.singleShot(1500, lambda: _run_selftest(script, tool))
        return tool


def _run_selftest(path: str, tool) -> None:
    """Developer hook: SAMSELECT_SELFTEST=/path/script.py runs a script inside Krita."""
    namespace = {"tool": tool, "Krita": Krita, "__file__": path}
    with open(path, encoding="utf-8") as f:
        code = compile(f.read(), path, "exec")
    exec(code, namespace)  # noqa: S102 - opt-in developer hook
