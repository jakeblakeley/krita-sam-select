"""SAM Select backend: serves SAM 3 (MLX) to the Krita plugin over stdin/stdout.

Launched by the plugin with the backend venv's Python. Frames (see
protocol.py) arrive on stdin and replies go to the *original* stdout; fd 1 is
redirected to stderr at startup so stray prints from libraries can never
corrupt the stream. The process exits when stdin closes, i.e. when Krita quits
or the plugin stops it.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import protocol  # noqa: E402

SERVER_VERSION = "0.1.0"


class Channel:
    def __init__(self) -> None:
        out_fd = os.dup(1)
        os.dup2(2, 1)  # anything written to fd 1 from now on lands in the log
        sys.stdout = sys.stderr
        self._out = os.fdopen(out_fd, "wb", buffering=0)
        self._in = sys.stdin.buffer
        self._lock = threading.Lock()

    def send(self, header: dict, payload: bytes = b"") -> None:
        data = protocol.encode(header, payload)
        with self._lock:
            view = memoryview(data)
            while view:
                n = self._out.write(view)
                view = view[n:]

    def receive(self):
        return protocol.read_frame(self._in)


def _watch_download(channel: Channel, repo: str, expected: int, stop: threading.Event) -> None:
    """Report download progress by watching the Hugging Face cache grow."""
    from huggingface_hub.constants import HF_HUB_CACHE

    folder = Path(HF_HUB_CACHE) / ("models--" + repo.replace("/", "--"))
    while not stop.wait(0.5):
        size = 0
        for p in folder.rglob("*"):
            try:
                if p.is_file() and not p.is_symlink():
                    size += p.stat().st_size
            except OSError:
                pass
        channel.send({"event": "status", "stage": "downloading", "progress": min(size / expected, 0.99)})


def main() -> int:
    channel = Channel()
    channel.send({"event": "hello", "server": SERVER_VERSION, "protocol": protocol.PROTOCOL_VERSION, "pid": os.getpid()})
    try:
        import numpy as np

        import engine as eng

        engine = eng.Sam3Engine()

        def progress(stage: str, value: float) -> None:
            channel.send({"event": "status", "stage": stage, "progress": value})

        stop = threading.Event()
        t0 = time.perf_counter()
        if not engine.is_cached():
            threading.Thread(
                target=_watch_download, args=(channel, engine.repo, eng.MODEL_BYTES, stop), daemon=True
            ).start()
        try:
            engine.load(progress)
        finally:
            stop.set()
        import mlx.core as mx

        info = mx.device_info()
        channel.send(
            {
                "event": "ready",
                "model": engine.repo,
                "device": info.get("device_name", "gpu"),
                "load_ms": round((time.perf_counter() - t0) * 1000),
            }
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        channel.send({"event": "fatal", "error": f"{type(exc).__name__}: {exc}"})
        return 1

    while True:
        frame = channel.receive()
        if frame is None:
            return 0
        header, payload = frame
        rid = header.get("id")
        cmd = header.get("cmd")
        t0 = time.perf_counter()
        try:
            if cmd == "shutdown":
                channel.send({"id": rid, "ok": True})
                return 0
            elif cmd == "ping":
                reply, data = {"ok": True}, b""
            elif cmd == "set_image":
                size = eng.IMAGE_SIZE
                rgb = np.frombuffer(payload, np.uint8).reshape(size, size, 3)
                state = engine.set_image(header["key"], rgb)
                reply, data = {"ok": True, "encode_ms": round(state.encode_ms)}, b""
            elif cmd == "segment":
                reply, data = _segment(engine, eng, header)
            else:
                raise ValueError(f"unknown command {cmd!r}")
        except KeyError as exc:
            reply, data = {"ok": False, "error": "unknown_image" if str(exc).strip("'") == header.get("key") else repr(exc)}, b""
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            reply, data = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, b""
        reply["id"] = rid
        reply["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        channel.send(reply, data)


def _segment(engine, eng, header: dict):
    state = engine.get_image(header["key"])
    prompt = header["prompt"]
    kind = prompt["type"]
    if kind == "point":
        result = engine.predict_point(state, [tuple(p) for p in prompt["points"]])
    elif kind == "box":
        box = tuple(prompt["box"])
        if prompt.get("objects", True):
            result = engine.predict_objects_in_box(state, box)
        else:
            result = engine.predict_box(state, box)
    elif kind == "text":
        box = tuple(prompt["box"]) if prompt.get("box") else None
        result = engine.predict_text(state, prompt["text"], float(prompt.get("threshold", 0.5)), box)
    else:
        raise ValueError(f"unknown prompt type {kind!r}")
    reply = {"ok": True, "score": round(result.score, 4), "count": result.count, "empty": True}
    if result.logits is None:
        return reply, b""
    out = header["out"]
    rendered = eng.render_mask(result.logits, int(out["w"]), int(out["h"]), bool(out.get("antialias", True)))
    if rendered is None:
        return reply, b""
    x, y, w, h, data = rendered
    reply.update(empty=False, x=x, y=y, w=w, h=h)
    return reply, data


if __name__ == "__main__":
    sys.exit(main())
