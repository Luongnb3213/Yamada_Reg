#!/usr/bin/env python3
"""Respring TẤT CẢ máy đang kết nối (clear RAM/compressor trước khi chạy batch).

Mỗi máy (song song): kill app Yamada nếu còn -> respring (kill SpringBoard, launchd tự
dựng lại, KHÔNG mất jailbreak) -> wake màn + chờ WiFi về. Tái dùng yamada_dom_runner.py
(tự re-exec sang Python có frida), nên chạy thẳng bằng python3 hệ thống cũng được:

    python3 scripts/respring_all.py                 # tất cả máy USB
    python3 scripts/respring_all.py 5f5b 64dda6c0    # chỉ vài máy (khớp tiền tố UDID)
"""
from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
DOM = str(ROOT_DIR / "scripts" / "yamada_dom_runner.py")


def list_usb_devices() -> list[str]:
    try:
        out = subprocess.run(["idevice_id", "-l"], capture_output=True, text=True, check=False).stdout
    except FileNotFoundError:
        print("[respring-all] Không thấy 'idevice_id' (cần libimobiledevice).", file=sys.stderr)
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def run_dom(action: str, device_id: str, timeout: float) -> subprocess.CompletedProcess:
    cmd = [sys.executable, DOM, "--action", action, "--device-id", device_id]
    return subprocess.run(cmd, cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=timeout, check=False)


def respring_one(device_id: str, results: dict) -> None:
    label = device_id[:8]
    # 1) Kill app Yamada nếu còn sót từ run trước (bỏ qua lỗi).
    try:
        run_dom("kill", device_id, timeout=40)
    except Exception:
        pass
    # 2) Respring + wake + network gate.
    try:
        proc = run_dom("respring", device_id, timeout=150)
        blob = (proc.stdout or "") + (proc.stderr or "")
        ok = proc.returncode == 0 and '"ok": true' in blob.lower()
        net = ""
        if "Mạng đã về sau" in blob:
            net = " | mạng về sau" + blob.rsplit("Mạng đã về sau", 1)[1].split("s", 1)[0] + "s"
        elif "Chưa xác nhận mạng" in blob:
            net = " | CẢNH BÁO: chưa xác nhận mạng về"
        results[device_id] = ok
        print(f"[respring-all] {label}: {'OK' if ok else 'LỖI'}{net}", flush=True)
        if not ok:
            print(f"[respring-all] {label} chi tiết: {blob.strip()[-200:]}", flush=True)
    except subprocess.TimeoutExpired:
        results[device_id] = False
        print(f"[respring-all] {label}: LỖI (timeout)", flush=True)
    except Exception as exc:
        results[device_id] = False
        print(f"[respring-all] {label}: LỖI ({exc})", flush=True)


def main() -> int:
    wanted = [a.strip().lower() for a in sys.argv[1:] if a.strip()]
    devices = list_usb_devices()
    if wanted:
        devices = [d for d in devices if any(d.lower().startswith(w) for w in wanted)]
    if not devices:
        print("[respring-all] Không có máy nào để respring.", file=sys.stderr)
        return 1
    print(f"[respring-all] Respring {len(devices)} máy song song: {', '.join(d[:8] for d in devices)}", flush=True)
    results: dict[str, bool] = {}
    threads = [threading.Thread(target=respring_one, args=(d, results), daemon=True) for d in devices]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok_count = sum(1 for v in results.values() if v)
    print(f"[respring-all] Xong: {ok_count}/{len(devices)} máy OK.", flush=True)
    return 0 if ok_count == len(devices) else 2


if __name__ == "__main__":
    raise SystemExit(main())
