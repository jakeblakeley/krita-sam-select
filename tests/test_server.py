"""End-to-end test of the backend server over its stdin/stdout protocol.

Run with the backend venv (needs the model, downloaded on first run):

    ~/Library/Application\\ Support/SamSelect/venv/bin/python tests/test_server.py [image]
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "src" / "samselect" / "server"
sys.path.insert(0, str(SERVER))

import protocol  # noqa: E402


class Client:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER / "sam_server.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
        )
        self.next_id = 0

    def event(self) -> dict:
        header, _ = protocol.read_frame(self.proc.stdout)
        return header

    def call(self, header: dict, payload: bytes = b""):
        self.next_id += 1
        header = dict(header, id=self.next_id)
        self.proc.stdin.write(protocol.encode(header, payload))
        self.proc.stdin.flush()
        while True:
            reply, data = protocol.read_frame(self.proc.stdout)
            if reply.get("id") == self.next_id:
                return reply, data

    def close(self) -> int:
        self.proc.stdin.close()
        return self.proc.wait(timeout=10)


def main() -> int:
    image_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if image_path is None:
        # Synthetic scene: two discs on a gradient.
        yy, xx = np.mgrid[0:600, 0:900]
        rgb = np.stack([xx / 900 * 120 + 60, yy / 600 * 120 + 60, np.full_like(xx, 110)], -1).astype(np.uint8)
        for cx, cy, r, col in [(250, 300, 120, (220, 40, 40)), (650, 280, 90, (40, 60, 220))]:
            rgb[(xx - cx) ** 2 + (yy - cy) ** 2 < r * r] = col
        image = Image.fromarray(rgb)
    else:
        image = Image.open(image_path).convert("RGB")
    W, H = image.size

    t0 = time.perf_counter()
    client = Client()
    hello = client.event()
    assert hello["event"] == "hello", hello
    while True:
        ev = client.event()
        if ev["event"] == "ready":
            break
        assert ev["event"] == "status", ev
    print(f"ready in {time.perf_counter() - t0:.1f}s on {ev['device']}")

    rgb = np.asarray(image.resize((1008, 1008), Image.BILINEAR), np.uint8)
    key = "test-image"
    reply, _ = client.call({"cmd": "segment", "key": key, "prompt": {"type": "point", "points": [[0.5, 0.5, 1]]}, "out": {"w": W, "h": H}})
    assert reply["ok"] is False and reply["error"] == "unknown_image", reply

    reply, _ = client.call({"cmd": "set_image", "key": key}, rgb.tobytes())
    assert reply["ok"], reply
    print(f"set_image: encode {reply['encode_ms']} ms (round trip {reply['ms']} ms)")

    prompts = {
        "click": {"type": "point", "points": [[250 / 900, 300 / 600, 1]]},
        "box (single)": {"type": "box", "box": [0.1, 0.2, 0.5, 0.8], "objects": False},
        "box (objects)": {"type": "box", "box": [0.05, 0.05, 0.95, 0.95]},
        "text": {"type": "text", "text": "circle"},
    }
    for name, prompt in prompts.items():
        t = time.perf_counter()
        reply, data = client.call({"cmd": "segment", "key": key, "prompt": prompt, "out": {"w": W, "h": H}})
        rt = (time.perf_counter() - t) * 1000
        assert reply["ok"], reply
        if not reply["empty"]:
            assert len(data) == reply["w"] * reply["h"], (len(data), reply)
        print(
            f"{name:14s} server {reply['ms']:6.1f} ms  round trip {rt:6.1f} ms  count {reply['count']}  "
            f"score {reply['score']:.3f}  roi {None if reply['empty'] else (reply['x'], reply['y'], reply['w'], reply['h'])}"
        )

    if image_path is None:
        # The click on the red disc should give roughly the disc's bbox.
        reply, data = client.call({"cmd": "segment", "key": key, "prompt": prompts["click"], "out": {"w": W, "h": H}})
        x, y, w, h = reply["x"], reply["y"], reply["w"], reply["h"]
        assert abs(x - 130) < 12 and abs(y - 180) < 12 and abs(w - 240) < 20 and abs(h - 240) < 20, (x, y, w, h)
        mask = np.frombuffer(data, np.uint8).reshape(h, w)
        assert mask[h // 2, w // 2] == 255 and mask[0, 0] == 0
        print("click geometry OK")

    assert client.close() == 0
    print("server exited cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
