from __future__ import annotations

import argparse
import json
import os
import queue
import shlex
import signal
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


def _load_config() -> dict:
    try:
        return json.loads((ROOT_DIR / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


CONFIG = _load_config()


def cfg(env_key: str, cfg_key: str, default, cast):
    """Nguồn giá trị theo thứ tự ưu tiên: env > config.json > mặc định.
    (CLI arg vẫn thắng tất cả vì argparse chỉ dùng default khi không truyền cờ.)"""
    raw = os.environ.get(env_key)
    if raw is None or raw == "":
        raw = CONFIG.get(cfg_key)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except Exception:
        return default


def _cfg_bool(env_key: str, cfg_key: str, default: bool) -> bool:
    raw = os.environ.get(env_key)
    if raw is None or raw == "":
        raw = CONFIG.get(cfg_key)
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in ("", "0", "false", "no")

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

# App Crane tụt đăng ký khỏi SpringBoard (không phải mất USB): iOS trả nil khi
# tìm com.opa334.CraneApplication. Cần uicache/respring/mở lại Crane trên máy đó.
# Nếu lặp liên tiếp -> loại máy ra để khỏi nướng cả loạt row (row còn lại tự chảy
# sang máy khỏe qua hàng đợi dùng chung).
CRANE_LOST_HINTS = (
    "returned nil for",
    "FBSApplicationLibrary",
    "unable to launch iOS app via FBS",
    "unable to find app with bundle identifier",
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
    parser.add_argument("--device-id", default=cfg("FRIDA_DEVICE_ID", "device_id", "auto", str), help="'auto', 'all', or comma-separated Frida device IDs.")
    parser.add_argument("--no-reload", action="store_true", help="Deprecated/default. Crane reload is already disabled by full-flow.")
    parser.add_argument("--reload-crane", action="store_true", help="Opt in to Crane reloadApplicationWithIdentifier after switching container.")
    parser.add_argument("--no-submit", action="store_true")
    parser.add_argument("--max-attempts", type=int, default=2, help="Max attempts per row, including the first run.")
    parser.add_argument("--device-error-threshold", type=int, default=1, help="Disable a device after this many consecutive hard lost-device errors.")
    parser.add_argument("--device-crane-error-threshold", type=int, default=3, help="Disable a device after this many consecutive 'Crane app mất đăng ký' errors (cần uicache/respring).")
    parser.add_argument("--soft-infra-cooldown-sec", type=float, default=15.0, help="Sleep this many seconds after Crane/DDI/connection errors before the device takes another row.")
    parser.add_argument("--device-start-gap-sec", type=float, default=2.0, help="Stagger worker start times to avoid hitting all USB/Frida devices at once.")
    parser.add_argument(
        "--row-timeout-sec",
        type=float,
        default=cfg("YAMADA_ROW_TIMEOUT_SEC", "row_timeout_sec", 180.0, float),
        help="WATCHDOG im lặng: nếu 1 row (full_flow) quá ngần này giây KHÔNG in thêm dòng log nào (frida-RPC treo vì app/WebView đơ) thì KILL cả cây tiến trình (full_flow -> dom_runner -> frida) và tính row LỖI, để không chẹn cả máy hàng giờ. Là ngưỡng IM LẶNG nên row chạy bình thường (vẫn in log đều) không bị giết oan dù chờ OTP hơi lâu. Mặc định 180 (3 phút). 0 = tắt watchdog.",
    )
    parser.add_argument(
        "--respring-every",
        type=int,
        default=cfg("YAMADA_RESPRING_EVERY", "respring_every", 30, int),
        help="Respring (kill SpringBoard) mỗi N row/máy GIỮA các row để xả RAM/compressor, chặn panic-reboot mất jailbreak. Sau respring tự wake màn. 0 = tắt. Mặc định 30 (hạ từ 50 vì 50 quá thưa, RAM kịp dồn gây reboot).",
    )
    parser.add_argument(
        "--respring-cooldown-sec",
        type=float,
        default=cfg("YAMADA_RESPRING_COOLDOWN", "respring_cooldown_sec", 5.0, float),
        help="Buffer nhỏ SAU respring trước khi máy nhận row kế. Việc CHỜ MẠNG VỀ giờ do NETWORK GATE trong respring lo (poll reachability tới khi internet thật về), nên đây chỉ là margin phòng reachability hơi lạc quan. Mặc định 5.",
    )
    parser.add_argument(
        "--respring-compressor-mb",
        type=float,
        default=cfg("YAMADA_RESPRING_COMP_MB", "respring_compressor_mb", 800.0, float),
        help="RESPRING THEO ÁP LỰC RAM: sau mỗi row đọc compressor của máy (host_statistics64); nếu vùng nén >= ngưỡng này (MB) thì respring NGAY, không chờ đủ N row. Đây là tín hiệu panic THẬT (free trên iOS luôn thấp ~30-40MB nên vô dụng làm ngưỡng; panic xảy ra khi compressed-pages chạm trần ~910MB trên máy 2GB). Mặc định 800 (~88 phần trăm trần, đã có margin). 0 = tắt đọc RAM (chỉ respring theo số row).",
    )
    parser.add_argument(
        "--weak-devices",
        default=cfg("YAMADA_WEAK_DEVICES", "weak_devices", "64dda6c0,5f5b,805deacc", str),
        help="Danh sách máy YẾU (hay reboot) phân tách dấu phẩy, khớp theo tiền tố UDID. Các máy này dùng --weak-respring-every (dày hơn) thay cho --respring-every. Mặc định 3 máy đã xác nhận panic launchd.",
    )
    parser.add_argument(
        "--weak-respring-every",
        type=int,
        default=cfg("YAMADA_WEAK_RESPRING_EVERY", "weak_respring_every", 15, int),
        help="Respring mỗi N row cho MÁY YẾU (xem --weak-devices). Nhỏ hơn --respring-every vì máy yếu dồn RAM nhanh hơn. Mặc định 15.",
    )
    parser.add_argument(
        "--log-compressor",
        action="store_true",
        help="In compressor (MB) của máy SAU MỖI ROW để theo dõi RAM bò lên theo số row (kể cả row respring theo số đếm). Cũng bật được qua config.json \"log_compressor\": true hoặc env YAMADA_LOG_COMPRESSOR=1. Tốn thêm 1 lần đọc RAM/row trên row mà lẽ ra không cần đọc.",
    )
    parser.add_argument("--list-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    # Đẩy slot throttle từ config.json xuống env cho tiến trình con (spawn_throttle.py /
    # crane_container_manager.py / dom_runner đọc trực tiếp từ env). Env sẵn có vẫn thắng.
    for env_key, cfg_key in (("YAMADA_SPAWN_SLOTS", "spawn_slots"), ("YAMADA_CRANE_SLOTS", "crane_slots")):
        if not os.environ.get(env_key) and CONFIG.get(cfg_key) not in (None, ""):
            os.environ[env_key] = str(CONFIG.get(cfg_key))
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
    if args.respring_every < 0:
        print("[batch] respring-every phải >= 0.", file=sys.stderr, flush=True)
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
    device_crane_errors: dict[str, int] = {device_id: 0 for device_id in device_ids}
    device_rows_done: dict[str, int] = {device_id: 0 for device_id in device_ids}
    respring_every = max(0, int(args.respring_every))
    weak_respring_every = max(0, int(args.weak_respring_every))
    comp_mb_threshold = max(0.0, float(args.respring_compressor_mb))
    row_timeout_sec = max(0.0, float(args.row_timeout_sec))
    log_compressor = bool(args.log_compressor) or _cfg_bool("YAMADA_LOG_COMPRESSOR", "log_compressor", False)
    weak_tokens = [t.strip().lower() for t in (args.weak_devices or "").split(",") if t.strip()]

    def _is_weak(dev: str) -> bool:
        dl = dev.lower()
        return any(dl.startswith(tok) for tok in weak_tokens)

    # Mỗi máy một ngưỡng số-row riêng: máy yếu respring dày hơn.
    respring_every_for: dict[str, int] = {
        dev: (weak_respring_every if _is_weak(dev) else respring_every) for dev in device_ids
    }
    # Bật cơ chế respring nếu có BẤT KỲ trigger nào: theo số row (bất kỳ máy nào) hoặc theo áp lực RAM.
    respring_enabled = (max(respring_every_for.values(), default=0) > 0) or (comp_mb_threshold > 0) or log_compressor
    frida_py = ""
    if respring_enabled:
        try:
            frida_py = frida_python()
        except Exception as exc:
            print(f"[batch] Không tìm được Python frida -> TẮT respring + log compressor: {exc}", flush=True)
            respring_enabled = False
            respring_every = 0
            comp_mb_threshold = 0.0
            log_compressor = False
    if respring_enabled:
        weak_in_batch = [device_label(d) for d in device_ids if _is_weak(d)]
        print(
            f"[batch] Respring: theo số row (thường {respring_every}/máy, yếu {weak_respring_every}/máy)"
            + (f" + theo áp lực RAM (compressor >= {comp_mb_threshold:.0f}MB)" if comp_mb_threshold > 0 else "")
            + (" + LOG compressor mỗi row" if log_compressor else "")
            + (f" | máy yếu trong batch: {', '.join(weak_in_batch)}" if weak_in_batch else ""),
            flush=True,
        )
    batch_start = time.monotonic()
    print_lock = threading.Lock()
    result_lock = threading.Lock()
    stop_all = threading.Event()
    # PIN CHẶT THEO GÁN: mỗi row CHỈ chạy trên đúng device ở cột frida_device_id.
    # Thay hàng đợi chung (work-stealing) bằng hàng đợi RIÊNG cho từng device.
    canon = {d.strip().lower(): d for d in device_ids}  # map UDID (lower) -> id device đang kết nối
    device_queues: dict[str, queue.Queue[dict]] = {}
    unroutable: list[dict] = []
    for task in tasks:
        dev_raw = str(task.get("device_id") or "").strip()
        dev = canon.get(dev_raw.lower())
        if not dev:
            unroutable.append(task)
            continue
        queued_task = dict(task)
        queued_task["container_mode"] = "create"
        device_queues.setdefault(dev, queue.Queue()).put(queued_task)
    active_device_ids = [d for d in device_ids if d in device_queues]
    total_tasks = sum(q.qsize() for q in device_queues.values())

    # Báo cáo định tuyến trước khi chạy
    idle_connected = [d for d in device_ids if d not in device_queues]
    if idle_connected:
        print(
            f"[batch] {len(idle_connected)} device kết nối nhưng KHÔNG có row gán -> bỏ qua "
            f"(vd máy lạ cắm nhầm): " + ", ".join(device_label(d) for d in idle_connected),
            flush=True,
        )
    if unroutable:
        by_dev: dict[str, int] = {}
        for t in unroutable:
            k = str(t.get("device_id") or "").strip() or "(trống)"
            by_dev[k] = by_dev.get(k, 0) + 1
        detail = ", ".join(
            f"{device_label(k) if k != '(trống)' else '(trống)'}:{n} row" for k, n in by_dev.items()
        )
        print(
            f"[batch] CẢNH BÁO: {len(unroutable)} row gán cho device KHÔNG kết nối / để trống "
            f"-> KHÔNG chạy (pin chặt): {detail}",
            flush=True,
        )
    print(
        f"[batch] Pin chặt: {len(active_device_ids)} device có việc | {total_tasks} row sẽ chạy.",
        flush=True,
    )

    def log(line: str = "") -> None:
        with print_lock:
            print(line, flush=True)

    def read_mem(device_id: str) -> dict | None:
        """Đọc RAM máy qua dom_runner --action meminfo -> {compressor_mb, free_mb}.
        compressor là tín hiệu panic thật (chạm trần ~910MB -> launchd chết). None nếu lỗi."""
        cmd = [frida_py, "scripts/yamada_dom_runner.py", "--action", "meminfo", "--device-id", device_id]
        try:
            completed = subprocess.run(cmd, cwd=str(ROOT_DIR), text=True, capture_output=True, timeout=40, check=False)
            if completed.returncode != 0:
                tail = ((completed.stderr or "") + (completed.stdout or "")).strip()[-160:]
                return {"compressor_mb": None, "free_mb": None, "err": f"exit={completed.returncode} {tail}"}
            # dom_runner in kết quả bằng json.dumps(indent=2) -> JSON đẹp NHIỀU DÒNG ra
            # stdout (các log khác đi stderr). Parse CẢ KHỐI, không quét từng dòng.
            out = (completed.stdout or "").strip()
            start, end = out.find("{"), out.rfind("}")
            if start == -1 or end <= start:
                return {"compressor_mb": None, "free_mb": None, "err": f"no-json: {out[-160:]}"}
            data = json.loads(out[start:end + 1])
            if data.get("ok"):
                return {"compressor_mb": data.get("compressor_mb"), "free_mb": data.get("free_mb")}
            return {"compressor_mb": None, "free_mb": None, "err": str(data.get("raw", ""))[-160:]}
        except Exception as exc:
            return {"compressor_mb": None, "free_mb": None, "err": f"{type(exc).__name__}: {exc}"}

    def respring_device(device_id: str, reason: str = "định kỳ") -> None:
        cmd = [frida_py, "scripts/yamada_dom_runner.py", "--action", "respring", "--device-id", device_id]
        log(
            f"[batch][{device_label(device_id)}] Respring ({reason}) "
            "để xả RAM/compressor, chặn panic-reboot mất jailbreak..."
        )
        try:
            # timeout rộng: respring giờ còn chờ NETWORK GATE (tới 60s) + 8s wait + ~20s poll SpringBoard.
            completed = subprocess.run(cmd, cwd=str(ROOT_DIR), text=True, capture_output=True, timeout=150, check=False)
            if completed.returncode != 0:
                out = ((completed.stdout or "") + (completed.stderr or "")).strip()
                log(f"[batch][{device_label(device_id)}] Respring lỗi (bỏ qua): {out[-200:]}")
            else:
                out = ((completed.stdout or "") + (completed.stderr or "")).strip()
                if "Mạng đã về sau" in out:
                    m = out.rsplit("Mạng đã về sau", 1)[1].split("s", 1)[0].strip()
                    log(f"[batch][{device_label(device_id)}] Network gate OK — mạng về sau{' ' + m}s, giao row kế.")
                elif "Chưa xác nhận mạng" in out:
                    log(f"[batch][{device_label(device_id)}] CẢNH BÁO: respring xong nhưng chưa xác nhận mạng về (gate timeout) — row kế có thể dính trang エラー.")
        except Exception as exc:
            log(f"[batch][{device_label(device_id)}] Respring lỗi tạm (bỏ qua): {exc}")

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
                start_new_session=True,  # cây tiến trình riêng -> watchdog kill được cả dom_runner/frida con
            )
            assert proc.stdout is not None
            # WATCHDOG: nếu row chạy quá row_timeout_sec (frida-RPC treo vì app/WebView đơ),
            # kill CẢ process-group để vòng đọc stdout thoát, khỏi chẹn máy hàng giờ.
            # WATCHDOG theo "im lặng": nếu quá row_timeout_sec KHÔNG có dòng output mới
            # (frida-RPC treo vì app/WebView đơ -> không in gì nữa) thì kill cả
            # process-group. Row chạy bình thường vẫn in log đều nên không bị giết oan
            # kể cả khi chờ OTP hơi lâu.
            row_timed_out = {"v": False}
            last_activity = [time.monotonic()]
            stop_watch = threading.Event()

            def _watch(p=proc, flag=row_timed_out, last=last_activity, stop=stop_watch):
                while not stop.wait(5.0):
                    if time.monotonic() - last[0] > row_timeout_sec:
                        flag["v"] = True
                        try:
                            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                        except Exception:
                            try:
                                p.kill()
                            except Exception:
                                pass
                        return

            watchdog = None
            if row_timeout_sec > 0:
                watchdog = threading.Thread(target=_watch, daemon=True)
                watchdog.start()
            output_lines: list[str] = []
            try:
                for line in proc.stdout:
                    last_activity[0] = time.monotonic()
                    output_lines.append(line)
                    log(f"[{device_label(device_id)} r{row}] {line.rstrip()}")
                final_code = proc.wait()
            finally:
                stop_watch.set()
            final_output = "".join(output_lines)
            if row_timed_out["v"]:
                final_code = final_code or 1
                log(f"[batch][{device_label(device_id)}] Row {row} TREO (im lặng > {row_timeout_sec:.0f}s) -> đã kill cây tiến trình, tính lỗi.")
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
        my_queue = device_queues[device_id]  # pin chặt: chỉ chạy row gán cho máy này
        while not stop_all.is_set():
            try:
                task = my_queue.get_nowait()
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
                    elif has_hint(final_output, CRANE_LOST_HINTS):
                        device_crane_errors[device_id] = device_crane_errors.get(device_id, 0) + 1
                        failures.append((row, final_code))
                        log(
                            f"[batch][{label}] App Crane mất đăng ký ở row {row} "
                            f"({device_crane_errors[device_id]}/{args.device_crane_error_threshold}); "
                            "cần uicache/respring máy này."
                        )
                        if device_crane_errors[device_id] >= args.device_crane_error_threshold:
                            disabled_devices.add(device_id)
                            log(
                                f"[batch][{label}] Loại device khỏi lượt chạy vì app Crane mất đăng ký "
                                f"{device_crane_errors[device_id]} lần liên tiếp (chạy uicache/respring rồi bật lại)."
                            )
                            return
                        cooldown_after = args.soft_infra_cooldown_sec
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
                    device_crane_errors[device_id] = 0
                    success_durations.append(row_elapsed)
                    avg = sum(success_durations) / len(success_durations)
                    log(f"[batch][{label}] Row {row} xong trong {row_elapsed:.1f}s | trung bình {avg:.1f}s/nick")
            # Footprint/RAM: respring GIỮA các row để chặn panic-reboot (mất jailbreak).
            # (1) ÁP LỰC RAM thật: vùng nén compressor chạm ngưỡng -> respring NGAY (bắt
            #     máy thrash trước khi chạm trần ~910MB gây panic, kể cả khi chưa đủ N row).
            # (2) LƯỚI ĐỠ theo SỐ ROW: mỗi N row/máy (N nhỏ hơn cho máy yếu).
            # App Yamada đã được full-flow kill cuối row. Máy bị loại đã return trước đó.
            if respring_enabled and not stop_all.is_set():
                with result_lock:
                    device_rows_done[device_id] += 1
                    rows_since_respring = device_rows_done[device_id]
                my_every = respring_every_for.get(device_id, respring_every)
                count_trigger = my_every > 0 and rows_since_respring >= my_every
                pressure_trigger = False
                comp_mb = None
                # Đọc RAM 1 lần nếu: cần cho log (mỗi row), HOẶC cần cho ngưỡng áp lực
                # (chỉ khi count chưa kích, tránh spawn thừa). Tái dùng cho cả hai.
                need_read = log_compressor or (comp_mb_threshold > 0 and not count_trigger)
                if need_read and not stop_all.is_set():
                    mem = read_mem(device_id)
                    comp_mb = mem.get("compressor_mb") if mem else None
                    if log_compressor:
                        if comp_mb is not None:
                            free_mb = mem.get("free_mb")
                            log(
                                f"[batch][{label}] RAM: compressor {comp_mb:.0f}MB"
                                + (f" | free {free_mb:.0f}MB" if free_mb is not None else "")
                                + f" | {rows_since_respring} row kể từ respring (ngưỡng {comp_mb_threshold:.0f}, trần ~910)"
                            )
                        else:
                            err = (mem or {}).get("err") if isinstance(mem, dict) else None
                            log(f"[batch][{label}] RAM: đọc compressor lỗi (bỏ qua)" + (f": {err}" if err else "."))
                    if comp_mb_threshold > 0 and comp_mb is not None and comp_mb >= comp_mb_threshold:
                        pressure_trigger = True
                if (count_trigger or pressure_trigger) and not stop_all.is_set():
                    reason = (
                        f"áp lực RAM: compressor {comp_mb:.0f}MB >= {comp_mb_threshold:.0f}MB (trần ~910)"
                        if pressure_trigger else f"đủ {my_every} row"
                    )
                    with result_lock:
                        device_rows_done[device_id] = 0  # reset: đếm lại từ lần respring này
                    respring_device(device_id, reason)
                    cooldown = max(0.0, float(args.respring_cooldown_sec))
                    if cooldown > 0 and not stop_all.is_set():
                        log(f"[batch][{device_label(device_id)}] Nghỉ {cooldown:.0f}s sau respring để chờ WiFi về trước row kế.")
                        time.sleep(cooldown)
            if cooldown_after > 0 and not stop_all.is_set():
                time.sleep(cooldown_after)

    threads = [
        threading.Thread(target=worker, args=(device_id, index), daemon=True)
        for index, device_id in enumerate(active_device_ids)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Gom row chưa chạy: pin chặt nên mỗi queue chỉ còn row nếu device đó bị
    # loại giữa chừng (mất Frida/Crane) hoặc batch dừng vì lỗi chung.
    remaining_rows: list[int] = []
    for dq in device_queues.values():
        while True:
            try:
                remaining_task = dq.get_nowait()
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
        print(
            f"[batch] Còn {len(remaining_rows)} row chưa chạy (device gán bị loại giữa chừng "
            f"hoặc batch dừng sớm): {preview}{suffix}",
            flush=True,
        )
    if unroutable:
        print(
            f"[batch] {len(unroutable)} row KHÔNG chạy vì device gán không kết nối / để trống "
            "(xem CẢNH BÁO định tuyến ở đầu).",
            flush=True,
        )
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
