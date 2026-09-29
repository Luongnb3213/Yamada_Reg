from __future__ import annotations

import argparse
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import openpyxl


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_FRIDA_PYTHON = "/Users/macbook/Library/Application Support/pipx/venvs/frida-tools/bin/python"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.connections.xlsx_connection import normalize_status  # noqa: E402
from yamada_profile_from_excel import cell_text, normalize_header  # noqa: E402


STOP_ON_ERROR_HINTS = (
    "Frida chưa thấy iPhone USB",
    "device not found",
    "unable to find process",
    "Không có CraneManager",
    "CraneManager unavailable",
    "Excel appears to be open/locked",
)


def quote_cmd(cmd: list[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in cmd)


def choose_sheet(wb, sheet_name: str):
    if sheet_name in wb.sheetnames:
        return wb[sheet_name]
    for fallback in ("Iclouds", "Gmails", "Outlooks", "Inputs"):
        if fallback in wb.sheetnames:
            return wb[fallback]
    return wb[wb.sheetnames[0]]


def runnable_rows(xlsx: Path, sheet_name: str, limit: int) -> tuple[str, list[dict]]:
    wb = openpyxl.load_workbook(xlsx, data_only=True)
    try:
        ws = choose_sheet(wb, sheet_name)
        rows = ws.iter_rows(values_only=True)
        try:
            header_row = next(rows)
        except StopIteration:
            return ws.title, []

        headers = [normalize_header(cell) for cell in header_row]
        if "email" not in headers:
            raise RuntimeError(f"Sheet {ws.title!r} không có cột email.")
        email_pos = headers.index("email")
        status_pos = headers.index("status") if "status" in headers else None
        device_pos = headers.index("frida_device_id") if "frida_device_id" in headers else None

        selected: list[dict] = []
        for row_number, row in enumerate(rows, start=2):
            email = cell_text(row[email_pos]) if email_pos < len(row) else ""
            if not email:
                continue
            status_raw = cell_text(row[status_pos]) if status_pos is not None and status_pos < len(row) else ""
            status = normalize_status(status_raw)
            if status not in ("", "PENDING", "FAILED"):
                continue
            preferred_device = cell_text(row[device_pos]) if device_pos is not None and device_pos < len(row) else ""
            selected.append({"row": row_number, "device_id": preferred_device})
            if limit > 0 and len(selected) >= limit:
                break
        return ws.title, selected
    finally:
        wb.close()


def frida_python() -> str:
    candidates = [
        os.environ.get("FRIDA_PYTHON", ""),
        DEFAULT_FRIDA_PYTHON,
        sys.executable,
        "python3",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            completed = subprocess.run(
                [candidate, "-c", "import frida"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if completed.returncode == 0:
                return candidate
        except OSError:
            continue
    raise RuntimeError("Không tìm thấy Python có module frida để liệt kê device.")


def list_usb_devices() -> list[dict]:
    code = (
        "import frida,json;"
        "print(json.dumps([{'id':d.id,'name':d.name,'type':d.type} "
        "for d in frida.enumerate_devices() if d.type=='usb']))"
    )
    completed = subprocess.run([frida_python(), "-c", code], text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "Không liệt kê được Frida devices.")
    return json.loads(completed.stdout or "[]")


def resolve_device_ids(value: str) -> list[str]:
    raw = (value or "auto").strip()
    if raw.lower() in ("all", "*", "tat-ca", "tất-cả"):
        devices = list_usb_devices()
        ids = [str(device.get("id") or "").strip() for device in devices if device.get("id")]
        if not ids:
            raise RuntimeError("Không thấy iPhone USB nào qua Frida.")
        return ids
    ids = [part.strip() for part in raw.split(",") if part.strip()]
    return ids or ["auto"]


def device_label(device_id: str) -> str:
    if device_id == "auto":
        return "auto"
    return device_id[:8]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Yamada full flow for multiple rows in one sheet.")
    parser.add_argument("--xlsx", required=True)
    parser.add_argument("--sheet", default="Iclouds")
    parser.add_argument("--limit", type=int, default=0, help="0 = run all runnable rows; N = run first N runnable rows.")
    parser.add_argument("--wait-timeout-ms", type=int, default=15000)
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--device-id", default=os.environ.get("FRIDA_DEVICE_ID", "auto"), help="'auto', 'all', or comma-separated Frida device IDs.")
    parser.add_argument("--no-reload", action="store_true")
    parser.add_argument("--no-submit", action="store_true")
    parser.add_argument("--max-attempts", type=int, default=2, help="Max attempts per row, including the first run.")
    parser.add_argument("--list-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    xlsx = Path(args.xlsx).expanduser()
    if not xlsx.exists():
        print(f"[batch] Không thấy file Excel: {xlsx}", file=sys.stderr, flush=True)
        return 1
    if args.limit < 0:
        print("[batch] Số nick phải >= 0.", file=sys.stderr, flush=True)
        return 1
    if args.max_attempts < 1:
        print("[batch] max-attempts phải >= 1.", file=sys.stderr, flush=True)
        return 1
    try:
        device_ids = resolve_device_ids(args.device_id)
    except Exception as exc:
        print(f"[batch] {exc}", file=sys.stderr, flush=True)
        return 1

    sheet, tasks = runnable_rows(xlsx, args.sheet, args.limit)
    print(f"[batch] Sheet={sheet} | số nick={'full' if args.limit == 0 else args.limit} | chọn {len(tasks)} row", flush=True)
    print(f"[batch] Devices: {', '.join(device_ids)}", flush=True)
    if tasks:
        preview = ", ".join(str(task["row"]) for task in tasks[:20])
        suffix = "..." if len(tasks) > 20 else ""
        print(f"[batch] Rows: {preview}{suffix}", flush=True)
    if args.list_only:
        return 0
    if not tasks:
        print("[batch] Không có row PENDING/FAILED/trống có email để chạy.", flush=True)
        return 0

    failures: list[tuple[int, int]] = []
    success_durations: list[float] = []
    batch_start = time.monotonic()
    print_lock = threading.Lock()
    result_lock = threading.Lock()
    stop_all = threading.Event()
    total_tasks = len(tasks)
    task_queues: dict[str, queue.Queue[dict]] = {device_id: queue.Queue() for device_id in device_ids}
    queued_per_device = {device_id: 0 for device_id in device_ids}
    rr = 0
    for task in tasks:
        preferred = str(task.get("device_id") or "")
        if preferred in task_queues:
            target = preferred
        else:
            target = device_ids[rr % len(device_ids)]
            rr += 1
        queued_task = dict(task)
        queued_task["container_mode"] = "active-then-create" if queued_per_device[target] == 0 else "create"
        queued_per_device[target] += 1
        task_queues[target].put(queued_task)

    def log(line: str = "") -> None:
        with print_lock:
            print(line, flush=True)

    def run_task(device_id: str, index: int, row: int, container_mode: str) -> tuple[int, str, float]:
        cmd = [
            sys.executable,
            "scripts/yamada_full_flow.py",
            "--xlsx",
            str(xlsx),
            "--sheet",
            sheet,
            "--row",
            str(row),
            "--wait-timeout-ms",
            str(args.wait_timeout_ms),
            "--max-steps",
            str(args.max_steps),
            "--device-id",
            device_id,
            "--container-mode",
            container_mode,
        ]
        if args.no_reload:
            cmd.append("--no-reload")
        if args.no_submit:
            cmd.append("--no-submit")

        final_code = 0
        final_output = ""
        row_start = time.monotonic()
        for attempt in range(1, args.max_attempts + 1):
            log(
                f"\n[batch][{device_label(device_id)}] ({index}/{total_tasks}) chạy row {row} "
                f"| container-mode={container_mode} | attempt {attempt}/{args.max_attempts}"
            )
            log(f"[batch][{device_label(device_id)}] $ " + quote_cmd(cmd))
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            output_lines: list[str] = []
            for line in proc.stdout:
                output_lines.append(line)
                log(f"[{device_label(device_id)} r{row}] {line.rstrip()}")
            final_code = proc.wait()
            final_output = "".join(output_lines)
            if final_code == 0:
                break
            if attempt < args.max_attempts:
                log(f"[batch][{device_label(device_id)}] Row {row} lỗi exit={final_code}, retry lần cuối...")
            if stop_all.is_set():
                break

        row_elapsed = time.monotonic() - row_start
        return final_code, final_output, row_elapsed

    counter_lock = threading.Lock()
    counter = {"value": 0}

    def worker(device_id: str) -> None:
        while not stop_all.is_set():
            try:
                task = task_queues[device_id].get_nowait()
            except queue.Empty:
                return
            with counter_lock:
                counter["value"] += 1
                index = counter["value"]
            row = int(task["row"])
            container_mode = str(task.get("container_mode") or "active-then-next")
            final_code, final_output, row_elapsed = run_task(device_id, index, row, container_mode)
            label = device_label(device_id)
            with result_lock:
                if final_code != 0:
                    failures.append((row, final_code))
                    if any(hint in final_output for hint in STOP_ON_ERROR_HINTS):
                        stop_all.set()
                        log(f"[batch][{label}] Dừng batch vì lỗi hạ tầng ở row {row} sau {args.max_attempts} attempt.")
                    else:
                        log(f"[batch][{label}] Row {row} lỗi exit={final_code} sau {args.max_attempts} attempt, chuyển row tiếp theo.")
                else:
                    success_durations.append(row_elapsed)
                    avg = sum(success_durations) / len(success_durations)
                    log(f"[batch][{label}] Row {row} xong trong {row_elapsed:.1f}s | trung bình {avg:.1f}s/nick")

    threads = [threading.Thread(target=worker, args=(device_id,), daemon=True) for device_id in device_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    if failures:
        detail = ", ".join(f"row {row}: exit {code}" for row, code in failures[:10])
        more = "..." if len(failures) > 10 else ""
        print(f"\n[batch] Xong, có {len(failures)} row lỗi: {detail}{more}", flush=True)
    else:
        print("\n[batch] Xong, không có row lỗi.", flush=True)
    if success_durations:
        total = time.monotonic() - batch_start
        avg = sum(success_durations) / len(success_durations)
        per_hour = 3600 / avg if avg > 0 else 0
        print(f"[batch] Thống kê: {len(success_durations)} nick OK | avg={avg:.1f}s/nick | ~{per_hour:.1f} nick/giờ | total={total:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
