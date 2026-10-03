"""Cross-process spawn throttle.

Khi chạy nhiều máy song song, mỗi row chỉ "căng" frida-server ở khoảnh khắc
spawn (Crane host + app) ~1-3s; phần còn lại (chờ OTP, chạy DOM) không đụng
frida-server. Module này giới hạn SỐ SPAWN ĐỒNG THỜI trên toàn máy (qua file
lock dùng chung giữa các process), KHÔNG giảm số máy chạy song song.

Knob: env YAMADA_SPAWN_SLOTS (mặc định 3). Có thể override khi gọi spawn_slot().
"""

from __future__ import annotations

import fcntl
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path


def _default_slots() -> int:
    try:
        return max(1, int(os.environ.get("YAMADA_SPAWN_SLOTS", "3") or "3"))
    except ValueError:
        return 3


def _slot_dir() -> Path:
    override = os.environ.get("YAMADA_SPAWN_SLOT_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[1] / "agents" / "runtime" / "spawn_slots"


@contextmanager
def spawn_slot(slots: int | None = None, timeout: float = 120.0, poll: float = 0.2, label: str = ""):
    """Giữ 1 slot spawn trong khối `with`. Tối đa `slots` process giữ slot cùng lúc.

    Fail-open: nếu quá `timeout` chưa lấy được slot thì chạy tiếp KHÔNG throttle,
    để không bao giờ khoá chết cả farm vì một lock kẹt.
    """
    n = max(1, int(slots if slots is not None else _default_slots()))
    directory = _slot_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Không tạo được thư mục lock -> bỏ qua throttle, không chặn công việc.
        yield
        return

    deadline = time.time() + max(0.0, float(timeout))
    fd: int | None = None
    announced = False
    while True:
        for i in range(n):
            path = directory / f"slot_{i}.lock"
            try:
                candidate = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
            except OSError:
                continue
            try:
                fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fd = candidate
                break
            except OSError:
                os.close(candidate)
        if fd is not None:
            break
        if time.time() >= deadline:
            if label:
                print(
                    f"[spawn-throttle] {label}: chờ slot quá {timeout:.0f}s, chạy không throttle.",
                    file=sys.stderr,
                )
            break
        if not announced and label:
            print(
                f"[spawn-throttle] {label}: chờ slot spawn (tối đa {n} máy cùng lúc)...",
                file=sys.stderr,
            )
            announced = True
        time.sleep(max(0.05, float(poll)))

    try:
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
