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


HARD_DEVICE_LOST_HINTS = (
    "Frida chưa thấy iPhone USB",
    "device not found",
)

ROW_INFRA_ERROR_HINTS = (
    "unable to find process",
    "Không có CraneManager",
    "CraneManager unavailable",
    "this feature requires an iOS Developer Disk Image",
    "the connection is closed",
    "tLS connection closed unexpectedly",
)

GLOBAL_ERROR_HINTS = (
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
    parser.add_argument("--no-reload", action="store_true", help="Deprecated/default. Crane reload is already disabled by full-flow.")
    parser.add_argument("--reload-crane", action="store_true", help="Opt in to Crane reloadApplicationWithIdentifier after switching container.")
    parser.add_argument("--no-submit", action="store_true")
    parser.add_argument("--max-attempts", type=int, default=2, help="Max attempts per row, including the first run.")
    parser.add_argument("--device-error-threshold", type=int, default=1, help="Disable a device after this many consecutive hard lost-device errors.")
    parser.add_argument("--soft-infra-cooldown-sec", type=float, default=15.0, help="Sleep this many seconds after Crane/DDI/connection errors before the device takes another row.")
    parser.add_argument("--device-start-gap-sec", type=float, default=2.0, help="Stagger worker start times to avoid hitting all USB/Frida devices at once.")
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
    if args.device_error_threshold < 1:
        print("[batch] device-error-threshold phải >= 1.", file=sys.stderr, flush=True)
        return 1
    if args.soft_infra_cooldown_sec < 0:
        print("[batch] soft-infra-cooldown-sec phải >= 0.", file=sys.stderr, flush=True)
        return 1
    if args.device_start_gap_sec < 0:
        print("[batch] device-start-gap-sec phải >= 0.", file=sys.stderr, flush=True)
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
    disabled_devices: set[str] = set()
    device_infra_errors: dict[str, int] = {device_id: 0 for device_id in device_ids}
    batch_start = time.monotonic()
    print_lock = threading.Lock()
    result_lock = threading.Lock()
    stop_all = threading.Event()
    total_tasks = len(tasks)
    task_queue: queue.Queue[dict] = queue.Queue()
    for task in tasks:
        queued_task = dict(task)
        queued_task["container_mode"] = "create"
        task_queue.put(queued_task)

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
        if args.reload_crane:
            cmd.append("--reload-crane")
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

    def has_hint(output: str, hints: tuple[str, ...]) -> bool:
        return any(hint in output for hint in hints)

    def worker(device_id: str, worker_index: int) -> None:
        start_delay = worker_index * args.device_start_gap_sec
        if start_delay > 0:
            log(f"[batch][{device_label(device_id)}] chờ {start_delay:.1f}s để giãn tải USB/Frida lúc bắt đầu.")
            time.sleep(start_delay)
        while not stop_all.is_set():
            try:
                task = task_queue.get_nowait()
            except queue.Empty:
                return
            with counter_lock:
                counter["value"] += 1
                index = counter["value"]
            row = int(task["row"])
            container_mode = str(task.get("container_mode") or "create")
            final_code, final_output, row_elapsed = run_task(device_id, index, row, container_mode)
            label = device_label(device_id)
            cooldown_after = 0.0
            with result_lock:
                if final_code != 0:
                    if has_hint(final_output, GLOBAL_ERROR_HINTS):
                        failures.append((row, final_code))
                        stop_all.set()
                        log(f"[batch][{label}] Dừng batch vì lỗi chung ở row {row} sau {args.max_attempts} attempt.")
                    elif has_hint(final_output, HARD_DEVICE_LOST_HINTS):
                        device_infra_errors[device_id] = device_infra_errors.get(device_id, 0) + 1
                        failures.append((row, final_code))
                        log(
                            f"[batch][{label}] Device mất Frida/USB ở row {row} "
                            f"({device_infra_errors[device_id]}/{args.device_error_threshold}); "
                            "không requeue row này."
                        )
                        if device_infra_errors[device_id] >= args.device_error_threshold:
                            disabled_devices.add(device_id)
                            log(
                                f"[batch][{label}] Loại device khỏi lượt chạy vì mất Frida/USB "
                                f"{device_infra_errors[device_id]} lần liên tiếp."
                            )
                            return
                    elif has_hint(final_output, ROW_INFRA_ERROR_HINTS):
                        failures.append((row, final_code))
                        cooldown_after = args.soft_infra_cooldown_sec
                        log(
                            f"[batch][{label}] Row {row} lỗi hạ tầng tạm thời "
                            f"(Crane/DDI/connection); không requeue row này, nghỉ {cooldown_after:.1f}s rồi chạy tiếp."
                        )
                    else:
                        failures.append((row, final_code))
                        log(f"[batch][{label}] Row {row} lỗi exit={final_code} sau {args.max_attempts} attempt, chuyển row tiếp theo.")
                else:
                    device_infra_errors[device_id] = 0
                    success_durations.append(row_elapsed)
                    avg = sum(success_durations) / len(success_durations)
                    log(f"[batch][{label}] Row {row} xong trong {row_elapsed:.1f}s | trung bình {avg:.1f}s/nick")
            if cooldown_after > 0 and not stop_all.is_set():
                time.sleep(cooldown_after)

    threads = [
        threading.Thread(target=worker, args=(device_id, index), daemon=True)
        for index, device_id in enumerate(device_ids)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    remaining_rows: list[int] = []
    while True:
        try:
            remaining_task = task_queue.get_nowait()
        except queue.Empty:
            break
        try:
            remaining_rows.append(int(remaining_task.get("row")))
        except (TypeError, ValueError):
            continue

    if disabled_devices:
        labels = ", ".join(device_label(device_id) for device_id in sorted(disabled_devices))
        print(f"\n[batch] Đã loại {len(disabled_devices)} device mất Frida/USB khỏi lượt chạy: {labels}", flush=True)
    if remaining_rows:
        preview = ", ".join(str(row) for row in remaining_rows[:20])
        suffix = "..." if len(remaining_rows) > 20 else ""
        print(f"[batch] Còn {len(remaining_rows)} row chưa chạy do hết device khả dụng: {preview}{suffix}", flush=True)
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
