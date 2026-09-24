"""Backend lifecycle: one-time install, server process, and request queue.

The SAM 3 model runs in a separate Python (a uv-managed venv) so that MLX,
the model weights and inference never block or destabilise Krita. Requests go
over the process's stdin/stdout using ``server/protocol.py`` frames.

Only one request is in flight at a time (the GPU is serial anyway). Requests
with a ``coalesce`` key replace any queued request with the same key, so a
fast-moving hover preview never builds a backlog.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path

from PyQt5.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, pyqtSignal

from .server import protocol

PLUGIN_DIR = Path(__file__).resolve().parent
SERVER_DIR = PLUGIN_DIR / "server"
LOCKFILE = SERVER_DIR / "requirements.lock"
APP_DIR = Path.home() / "Library" / "Application Support" / "SamSelect"
VENV_DIR = APP_DIR / "venv"
VENV_PYTHON = VENV_DIR / "bin" / "python"
MARKER = APP_DIR / "installed.txt"
LOG_DIR = Path.home() / "Library" / "Logs" / "SamSelect"
PYTHON_VERSION = "3.12"
UV_URL = "https://github.com/astral-sh/uv/releases/latest/download/uv-aarch64-apple-darwin.tar.gz"
UV_CANDIDATES = [
    APP_DIR / "bin" / "uv",
    Path("/opt/homebrew/bin/uv"),
    Path("/usr/local/bin/uv"),
    Path.home() / ".local" / "bin" / "uv",
    Path.home() / ".cargo" / "bin" / "uv",
]

# States, in the order a cold start walks through them.
MISSING = "missing"  # backend not installed yet
INSTALLING = "installing"
STOPPED = "stopped"  # installed, process not running
STARTING = "starting"
DOWNLOADING = "downloading"  # first run: fetching model weights
LOADING = "loading"
READY = "ready"
ERROR = "error"

BUSY_STATES = {INSTALLING, STARTING, DOWNLOADING, LOADING}


def _lock_digest() -> str:
    return hashlib.sha256(LOCKFILE.read_bytes()).hexdigest()[:16]


def find_uv() -> Path | None:
    for p in UV_CANDIDATES:
        if p.is_file() and os.access(p, os.X_OK):
            return p
    found = shutil.which("uv")
    return Path(found) if found else None


class _Request:
    __slots__ = ("header", "payload", "callback", "coalesce", "sent_at")

    def __init__(self, header, payload, callback, coalesce):
        self.header = header
        self.payload = payload
        self.callback = callback
        self.coalesce = coalesce
        self.sent_at = 0.0


class Backend(QObject):
    """Process-wide singleton; all windows share one server."""

    stateChanged = pyqtSignal(str, str, float)  # state, message, progress (0..1, or -1 for indeterminate)

    _instance: "Backend | None" = None

    @classmethod
    def instance(cls) -> "Backend":
        if cls._instance is None:
            cls._instance = Backend()
        return cls._instance

    def __init__(self) -> None:
        super().__init__()
        self.state = STOPPED if self.is_installed() else MISSING
        self.message = ""
        self.progress = -1.0
        self.device = ""
        self._proc: QProcess | None = None
        self._reader = protocol.FrameReader()
        self._queue: list[_Request] = []
        self._inflight: _Request | None = None
        self._next_id = 0
        self._known_images: list[str] = []  # LRU of image keys the server holds
        self._log_file = None
        self._install_steps: list = []
        self._installer: QProcess | None = None
        self.idle_minutes = 20
        self._idled = False
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.timeout.connect(self._on_idle)

    # ----------------------------------------------------------------- state

    def is_installed(self) -> bool:
        try:
            return VENV_PYTHON.exists() and MARKER.read_text().strip() == _lock_digest()
        except OSError:
            return False

    def _set_state(self, state: str, message: str = "", progress: float = -1.0) -> None:
        self.state, self.message, self.progress = state, message, progress
        self.stateChanged.emit(state, message, progress)

    @property
    def ready(self) -> bool:
        return self.state == READY

    def log_path(self) -> Path:
        return LOG_DIR / "server.log"

    def _log(self, text: str) -> None:
        try:
            if self._log_file is None:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                self._log_file = open(self.log_path(), "a", buffering=1, encoding="utf-8")
            self._log_file.write(text if text.endswith("\n") else text + "\n")
        except OSError:
            pass

    def _tail_log(self, lines: int = 6) -> str:
        try:
            return "\n".join(self.log_path().read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return ""

    # --------------------------------------------------------------- install

    def install(self) -> None:
        """Create the venv with uv (downloading uv itself if needed)."""
        if self.state == INSTALLING:
            return
        self.stop()
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self._log(f"=== install {time.ctime()} ===")
        steps = []
        uv = find_uv()
        if uv is None:
            uv = APP_DIR / "bin" / "uv"
            (APP_DIR / "bin").mkdir(parents=True, exist_ok=True)
            tarball = APP_DIR / "uv.tar.gz"
            steps.append(("Downloading uv…", "/usr/bin/curl", ["-fsSL", "-o", str(tarball), UV_URL]))
            steps.append(
                ("Unpacking uv…", "/usr/bin/tar", ["-xzf", str(tarball), "-C", str(APP_DIR / "bin"), "--strip-components", "1"])
            )
        steps.append(
            ("Creating Python environment…", str(uv), ["venv", "--allow-existing", "--python", PYTHON_VERSION, str(VENV_DIR)])
        )
        steps.append(
            (
                "Installing MLX and SAM 3 runtime (~350 MB)…",
                str(uv),
                ["pip", "install", "--python", str(VENV_PYTHON), "--no-deps", "-r", str(LOCKFILE)],
            )
        )
        self._install_steps = steps
        self._install_total = len(steps)
        self._run_next_install_step()

    def _run_next_install_step(self) -> None:
        if not self._install_steps:
            MARKER.write_text(_lock_digest())
            self._log("install complete")
            self._set_state(STOPPED, "Installed")
            self.start()
            return
        label, program, args = self._install_steps.pop(0)
        done = self._install_total - len(self._install_steps) - 1
        self._set_state(INSTALLING, label, done / self._install_total)
        self._log(f"$ {program} {' '.join(args)}")
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.setProcessEnvironment(self._environment())
        proc.readyReadStandardOutput.connect(lambda: self._log(bytes(proc.readAllStandardOutput()).decode(errors="replace")))
        proc.finished.connect(lambda code, status: self._install_step_done(proc, label, code, status))
        proc.errorOccurred.connect(lambda err: self._install_step_done(proc, label, -1, err) if err == QProcess.FailedToStart else None)
        self._installer = proc
        proc.start(program, args)

    def _install_step_done(self, proc, label, code, status) -> None:
        if proc is not self._installer:
            return
        self._installer = None
        proc.deleteLater()
        if code != 0:
            self._set_state(ERROR, f"Setup failed during “{label.rstrip('…')}”. See the log for details.\n{self._tail_log(3)}")
            return
        self._run_next_install_step()

    def _environment(self) -> QProcessEnvironment:
        env = QProcessEnvironment.systemEnvironment()
        # Krita's embedded Python must not leak into the backend interpreter.
        for var in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__"):
            env.remove(var)
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("HF_HUB_DISABLE_TELEMETRY", "1")
        env.insert("TOKENIZERS_PARALLELISM", "false")
        return env

    # ----------------------------------------------------------------- server

    def start(self) -> None:
        if self._proc is not None or self.state in (INSTALLING,):
            return
        if not self.is_installed():
            self._set_state(MISSING, "One-time setup required")
            return
        self._reader = protocol.FrameReader()
        self._known_images.clear()
        proc = QProcess(self)
        proc.setProcessEnvironment(self._environment())
        proc.setProcessChannelMode(QProcess.SeparateChannels)
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(lambda: self._log(bytes(proc.readAllStandardError()).decode(errors="replace")))
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_process_error)
        self._proc = proc
        self._log(f"=== start {time.ctime()} ===")
        self._set_state(STARTING, "Starting SAM 3…")
        proc.start(str(VENV_PYTHON), [str(SERVER_DIR / "sam_server.py")])

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        self._fail_pending("Backend stopped")
        if proc is not None:
            proc.finished.disconnect()
            proc.closeWriteChannel()  # the server exits on stdin EOF
            if not proc.waitForFinished(1500):
                proc.kill()
                proc.waitForFinished(500)
            proc.deleteLater()
        if self.state not in (MISSING, INSTALLING):
            self._set_state(STOPPED if self.is_installed() else MISSING)

    def ensure_started(self) -> None:
        if self.state in (STOPPED, ERROR) and self.is_installed():
            self.start()

    def _on_process_error(self, error) -> None:
        if error == QProcess.FailedToStart:
            self._proc = None
            self._set_state(ERROR, "Could not launch the backend Python. Try “Reinstall”.")

    def _on_finished(self, code, _status) -> None:
        self._proc = None
        self._fail_pending("Backend exited")
        if self.state == ERROR:
            return
        if code == 0:
            self._set_state(STOPPED, "Unloaded to free memory" if self._idled else "")
        else:
            self._set_state(ERROR, f"Backend exited unexpectedly (code {code}).\n{self._tail_log(3)}")
        self._idled = False

    def _on_idle(self) -> None:
        if self._proc is not None and self._inflight is None and not self._queue:
            self._idled = True
            self.stop()

    def _touch(self) -> None:
        if self.idle_minutes > 0:
            self._idle.start(int(self.idle_minutes * 60_000))

    # ---------------------------------------------------------------- frames

    def _on_stdout(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            frames = self._reader.feed(bytes(proc.readAllStandardOutput()))
        except protocol.ProtocolError as exc:
            self._log(f"protocol error: {exc}")
            self._set_state(ERROR, "Lost sync with the backend; restarting.")
            self.stop()
            return
        for header, payload in frames:
            if "event" in header:
                self._on_event(header)
            else:
                self._on_reply(header, payload)

    def _on_event(self, ev: dict) -> None:
        kind = ev["event"]
        if kind == "status":
            stage = ev.get("stage", "")
            progress = float(ev.get("progress", -1))
            if stage == "downloading":
                self._set_state(DOWNLOADING, "Downloading SAM 3 weights (1.7 GB, first run only)…", progress)
            else:
                self._set_state(LOADING, f"SAM 3: {stage}…")
        elif kind == "ready":
            self.device = ev.get("device", "")
            self._set_state(READY, f"Ready on {self.device}")
            self._touch()
            self._pump()
        elif kind == "fatal":
            self._set_state(ERROR, ev.get("error", "Backend failed to load"))

    def _on_reply(self, header: dict, payload: bytes) -> None:
        req, self._inflight = self._inflight, None
        if req is None or header.get("id") != req.header["id"]:
            self._log(f"unexpected reply {header}")
        else:
            if req.header.get("cmd") == "set_image" and header.get("ok"):
                self._remember_image(req.header["key"])
            if header.get("error") == "unknown_image":
                self._forget_image(req.header.get("key"))
            if req.callback:
                try:
                    req.callback(header, payload)
                except Exception:  # noqa: BLE001 - a UI callback must not kill the queue
                    import traceback

                    self._log(traceback.format_exc())
        self._touch()
        self._pump()

    def _fail_pending(self, reason: str) -> None:
        pending = ([self._inflight] if self._inflight else []) + self._queue
        self._inflight, self._queue = None, []
        for req in pending:
            if req.callback:
                try:
                    req.callback({"ok": False, "error": reason}, b"")
                except Exception:  # noqa: BLE001
                    pass

    # ---------------------------------------------------------------- requests

    def has_image(self, key: str) -> bool:
        return key in self._known_images

    def _remember_image(self, key: str) -> None:
        if key in self._known_images:
            self._known_images.remove(key)
        self._known_images.append(key)
        del self._known_images[:-3]  # the server keeps the 3 most recent

    def _forget_image(self, key) -> None:
        if key in self._known_images:
            self._known_images.remove(key)

    def send_image(self, key: str, rgb: bytes, callback=None) -> None:
        """Queue an image upload unless the server already has it (or it is queued)."""
        if self.has_image(key) or any(r.header.get("key") == key and r.header["cmd"] == "set_image" for r in self._queue):
            return
        if self._inflight is not None and self._inflight.header.get("cmd") == "set_image" and self._inflight.header.get("key") == key:
            return
        self.request({"cmd": "set_image", "key": key}, rgb, callback)

    def request(self, header: dict, payload: bytes = b"", callback=None, coalesce: str | None = None) -> None:
        req = _Request(dict(header), payload, callback, coalesce)
        if coalesce is not None:
            for i, queued in enumerate(self._queue):
                if queued.coalesce == coalesce:
                    self._queue[i] = req
                    self._pump()
                    return
        self._queue.append(req)
        if self.state in (STOPPED, ERROR):
            self.ensure_started()
        self._pump()

    def drop(self, coalesce: str) -> None:
        """Forget queued (not in-flight) requests with this coalesce key."""
        self._queue = [r for r in self._queue if r.coalesce != coalesce]

    def busy(self) -> bool:
        return self._inflight is not None and self._inflight.coalesce is None

    def _pump(self) -> None:
        if self._inflight is not None or not self._queue or self.state != READY or self._proc is None:
            return
        req = self._queue.pop(0)
        self._next_id += 1
        req.header["id"] = self._next_id
        req.sent_at = time.perf_counter()
        self._inflight = req
        self._proc.write(protocol.encode(req.header, req.payload))

    def shutdown(self) -> None:
        self._idle.stop()
        if self._installer is not None:
            self._installer.kill()
        self.stop()
