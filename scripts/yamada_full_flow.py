from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import openpyxl


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.connections.xlsx_connection import excel_write_lock  # noqa: E402


def quote_cmd(cmd: list[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in cmd)


def run_cmd(cmd: list[str], title: str) -> str:
    return run_cmd_output(cmd, title, stream=True)


def run_cmd_output(cmd: list[str], title: str, stream: bool = False) -> str:
    print(f"\n--- {title} ---", flush=True)
    print("$ " + quote_cmd(cmd), flush=True)
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    lines: list[str] = []
    for line in proc.stdout:
        lines.append(line)
        if stream:
            print(line, end="", flush=True)
    code = proc.wait()
    output = "".join(lines)
    if code != 0:
        if not stream and output.strip():
            print(output.strip(), flush=True)
        raise RuntimeError(f"{title} lỗi exit={code}.")
    return output


def parse_json_from_output(output: str) -> Any:
    decoder = json.JSONDecoder()
    candidates: list[tuple[int, Any]] = []
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        candidates.append((end, value))
    if not candidates:
        raise RuntimeError("Không đọc được JSON output từ DOM runner.")
    return max(candidates, key=lambda item: item[0])[1]


def walk(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def needs_auth_code(result: Any) -> bool:
    for node in walk(result):
        if not isinstance(node, dict):
            continue
        if node.get("wait") == "auth_code":
            return True
        if node.get("state") == "email_auth_code_input" and node.get("action") == "need_auth_code":
            return True
    return False


def first_dom_error(result: Any) -> str:
    for node in walk(result):
        if not isinstance(node, dict):
            continue
        if node.get("ok", True) is False and node.get("wait") != "auth_code":
            reason = node.get("reason") or node.get("error") or node.get("action") or node.get("state")
            return str(reason or "DOM stopped with ok=false.")
    return ""


def member_info_filled_without_submit(result: Any) -> bool:
    for node in walk(result):
        if isinstance(node, dict) and node.get("action") == "fill_member_info_no_submit":
            return True
    return False


def maybe_complete(result: Any) -> bool:
    for node in walk(result):
        if isinstance(node, dict) and node.get("state") == "maybe_complete":
            return True
    return False


def registration_done(result: Any) -> bool:
    for node in walk(result):
        if not isinstance(node, dict):
            continue
        if node.get("state") == "member_register_complete":
            return True
        if node.get("action") == "launch_app_after_registration":
            return True
        if node.get("action") == "already_logged_in_home":
            return True
    return maybe_complete(result)


def already_logged_in(result: Any) -> bool:
    for node in walk(result):
        if not isinstance(node, dict):
            continue
        if node.get("state") == "app_home_logged_in":
            return True
        if node.get("action") == "already_logged_in_home":
            return True
        if node.get("already_logged_in") is True:
            return True
    return False


def dom_history(result: Any) -> list[dict]:
    if isinstance(result, dict) and isinstance(result.get("history"), list):
        return [node for node in result["history"] if isinstance(node, dict)]
    return [node for node in walk(result) if isinstance(node, dict) and "state" in node and "action" in node]


def last_dom_step(result: Any) -> dict:
    history = dom_history(result)
    return history[-1] if history else {}


def print_dom_summary(result: Any, title: str) -> None:
    history = dom_history(result)
    if not history:
        print(f"[dom] {title}: không có history.", flush=True)
        return
    print(f"[dom] {title}: {len(history)} bước", flush=True)
    for idx, step in enumerate(history, start=1):
        state = step.get("state") or "-"
        action = step.get("action") or "-"
        extra = ""
        if step.get("wait") == "auth_code":
            extra = " | cần OTP"
        if action == "fill_member_info_no_submit":
            extra = " | đã fill thông tin, dừng trước submit"
        if action == "already_logged_in_home":
            extra = " | đã đăng nhập"
        if action == "wait_timeout":
            last = step.get("last") or {}
            extra = f" | timeout ở {last.get('state') or '?'}"
        print(f"[dom] {idx}. {state} -> {action}{extra}", flush=True)


def summarize_json_result(output: str) -> Any:
    result = parse_json_from_output(output)
    return result


def ensure_status_headers(ws) -> dict[str, int]:
    headers = [str(cell.value or "").strip() for cell in ws[1]]
    if not any(headers):
        headers = ["email", "status", "error_details"]
        ws.append(headers)
    for header in ("status", "error_details"):
        if header not in headers:
            ws.cell(row=1, column=len(headers) + 1, value=header)
            headers.append(header)
    return {header: index + 1 for index, header in enumerate(headers) if header}


def write_row_status(xlsx: Path, sheet_name: str, row: int, status: str, error_details: str = "") -> None:
    with excel_write_lock(xlsx):
        lock_path = xlsx.parent / f".~lock.{xlsx.name}#"
        if lock_path.exists():
            print(f"[excel] Không ghi được status vì Excel đang mở/lock: {lock_path}", flush=True)
            return
        wb = openpyxl.load_workbook(xlsx)
        try:
            if sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
            elif "Iclouds" in wb.sheetnames:
                ws = wb["Iclouds"]
            elif "Gmails" in wb.sheetnames:
                ws = wb["Gmails"]
            elif "Outlooks" in wb.sheetnames:
                ws = wb["Outlooks"]
            else:
                ws = wb[wb.sheetnames[0]]
            col = ensure_status_headers(ws)
            ws.cell(row=row, column=col["status"], value=status)
            ws.cell(row=row, column=col["error_details"], value=error_details)
            tmp = xlsx.with_name(f"{xlsx.stem}.{os.getpid()}.tmp.xlsx")
            wb.save(tmp)
            tmp.replace(xlsx)
        finally:
            wb.close()


def build_dom_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [
        sys.executable,
        "scripts/yamada_dom_runner.py",
        "--action",
        "run",
        "--wait-timeout-ms",
        str(args.wait_timeout_ms),
        "--max-steps",
        str(args.max_steps),
        "--device-id",
        args.device_id,
        "--profile-js",
        str(args.profile_js),
    ]
    if args.no_submit:
        cmd.append("--no-submit")
    return cmd


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one Yamada row end-to-end, including email OTP when needed.")
    parser.add_argument("--xlsx", required=True)
    parser.add_argument("--sheet", default="Iclouds")
    parser.add_argument("--row", type=int, required=True)
    parser.add_argument("--no-reload", action="store_true")
    parser.add_argument("--no-submit", action="store_true")
    parser.add_argument("--device-id", default=os.environ.get("FRIDA_DEVICE_ID", "auto"))
    parser.add_argument(
        "--container-mode",
        choices=["active-then-create", "active-then-next", "next-active", "create"],
        default="active-then-create",
    )
    parser.add_argument("--profile-js", default="")
    parser.add_argument("--wait-timeout-ms", type=int, default=15000)
    parser.add_argument("--max-steps", type=int, default=20)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    xlsx = Path(args.xlsx).expanduser()
    row_args = ["--xlsx", str(xlsx), "--sheet", args.sheet, "--row", str(args.row)]
    if args.profile_js:
        profile_js = Path(args.profile_js).expanduser()
    else:
        safe_device = re.sub(r"[^A-Za-z0-9_.-]+", "_", args.device_id or "auto")[:48]
        profile_js = ROOT_DIR / "agents" / "runtime" / f"current_profile_{safe_device}_r{args.row}_{os.getpid()}.js"
    args.profile_js = str(profile_js)

    try:
        crane_cmd = [
            sys.executable,
            "scripts/crane_container_manager.py",
            "ensure-row",
            *row_args,
            "--device-id",
            args.device_id,
            "--container-mode",
            args.container_mode,
        ]
        if args.no_reload:
            crane_cmd.append("--no-reload")
        crane_output = run_cmd_output(crane_cmd, "Chuẩn bị container")
        crane_result = summarize_json_result(crane_output)
        print(
            "[crane] container="
            f"{crane_result.get('crane_container_name') or crane_result.get('active_container_name') or crane_result.get('crane_container_label') or crane_result.get('active_container_label') or ''} "
            f"({crane_result.get('crane_container_id') or crane_result.get('active_container_id') or ''})",
            flush=True,
        )

        profile_output = run_cmd_output(
            [
                sys.executable,
                "scripts/yamada_profile_from_excel.py",
                *row_args,
                "--out",
                str(profile_js),
            ],
            "Đọc data từ Excel",
        )
        profile_result = summarize_json_result(profile_output)
        profile = profile_result.get("profile") if isinstance(profile_result, dict) else {}
        print(f"[excel] email={profile.get('email') or ''} row={args.row}", flush=True)

        first_output = run_cmd_output(build_dom_cmd(args), "Chạy DOM")
        first_result = parse_json_from_output(first_output)
        print_dom_summary(first_result, "lượt đầu")
        error = first_dom_error(first_result)
        if error:
            raise RuntimeError(error)

        final_result = first_result
        if needs_auth_code(first_result):
            print("\n[flow] App đang chờ OTP. Tự lấy OTP từ mailbox trong Excel...", flush=True)
            otp_output = run_cmd_output(
                [
                    sys.executable,
                    "scripts/fetch_yamada_email_otp.py",
                    *row_args,
                    "--write-excel",
                    "--profile-js",
                    str(profile_js),
                ],
                "Lấy OTP email",
            )
            otp_result = summarize_json_result(otp_output)
            print(f"[email] OTP={otp_result.get('auth_code') or '(không có)'}", flush=True)
            second_output = run_cmd_output(build_dom_cmd(args), "Chạy tiếp sau OTP")
            second_result = parse_json_from_output(second_output)
            print_dom_summary(second_result, "sau OTP")
            final_result = second_result
            if needs_auth_code(second_result):
                raise RuntimeError("Đã lấy OTP nhưng app vẫn đang chờ auth_code.")
            error = first_dom_error(second_result)
            if error:
                raise RuntimeError(error)

        if not registration_done(final_result):
            last = last_dom_step(final_result)
            raise RuntimeError(
                "DOM dừng trước màn thông tin/OTP: "
                f"state={last.get('state') or '?'} action={last.get('action') or '?'}"
            )

        status = "LOGGED_IN" if already_logged_in(final_result) else "SUCCESS"
        write_row_status(xlsx, args.sheet, args.row, status, "")
        print(f"\n[flow] Xong lượt chạy row {args.row} lúc {datetime.now():%Y-%m-%d %H:%M:%S}", flush=True)
        return 0
    except Exception as exc:
        message = str(exc)
        if "Lấy OTP email lỗi" in message:
            message = "Không lấy được OTP email. Kiểm tra otp_email/otp_pass hoặc mail OTP chưa về."
        write_row_status(xlsx, args.sheet, args.row, "FAILED", message)
        print(f"[flow] Lỗi: {message}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
