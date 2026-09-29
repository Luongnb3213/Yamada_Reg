from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_FRIDA_PYTHON = "/Users/macbook/Library/Application Support/pipx/venvs/frida-tools/bin/python"
DEFAULT_APP_ID = "jp.co.unisys.yamadamobile"


def import_frida():
    try:
        import frida  # type: ignore
    except ModuleNotFoundError:
        return None
    return frida


def candidate_frida_pythons() -> list[str]:
    candidates = [
        os.environ.get("FRIDA_PYTHON", ""),
        DEFAULT_FRIDA_PYTHON,
        shutil.which("python3") or "",
        sys.executable,
    ]
    seen: set[str] = set()
    out: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        path = str(Path(candidate).expanduser())
        if path in seen or not Path(path).exists():
            continue
        seen.add(path)
        out.append(path)
    return out


def can_import_frida(python_bin: str) -> bool:
    try:
        return subprocess.run(
            [python_bin, "-c", "import frida"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
    except OSError:
        return False


def find_frida_python() -> str:
    for python_bin in candidate_frida_pythons():
        if can_import_frida(python_bin):
            return python_bin
    raise RuntimeError("Không tìm thấy Python có module frida. Set FRIDA_PYTHON nếu cần.")


def frida_device_summary(frida) -> str:
    try:
        devices = frida.enumerate_devices()
    except Exception as exc:
        return f"không enumerate được device: {exc}"
    if not devices:
        return "không có device nào"
    return ", ".join(f"{device.id}/{device.name}/{device.type}" for device in devices)


def resolve_frida_device(frida, device_id: str, timeout: int = 10):
    if device_id and device_id.lower() != "auto":
        return frida.get_device(device_id)

    last_error = None
    for attempt in range(1, 4):
        try:
            return frida.get_usb_device(timeout=timeout if attempt == 1 else 3)
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                print(f"[yamada-dom] Chưa thấy iPhone USB qua Frida, thử lại {attempt}/3...", file=sys.stderr)
                time.sleep(1.5)

    devices = frida_device_summary(frida)
    raise RuntimeError(
        "Frida chưa thấy iPhone USB. Kiểm tra cáp/Trust This Computer/frida-server trên iPhone. "
        f"Devices hiện thấy: {devices}. Lỗi gốc: {last_error}"
    )


def json_print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def load_combined_agent(profile_js: Path) -> str:
    agent_path = ROOT_DIR / "agents" / "yamada_register_agent.js"
    if not agent_path.exists():
        raise RuntimeError(f"Agent not found: {agent_path}")
    if not profile_js.exists():
        raise RuntimeError(f"Profile JS not found: {profile_js}")
    return (
        agent_path.read_text(encoding="utf-8")
        + "\n\n"
        + profile_js.read_text(encoding="utf-8")
    )


def parse_result(raw: object) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw or ""))
    except json.JSONDecodeError:
        return {"ok": False, "raw": raw}


def app_pid(device, bundle_id: str) -> int | None:
    if not bundle_id:
        return None
    try:
        apps = device.enumerate_applications()
    except Exception:
        return None
    for app in apps:
        if getattr(app, "identifier", "") != bundle_id:
            continue
        pid = int(getattr(app, "pid", 0) or 0)
        return pid if pid > 0 else None
    return None


def attach_or_launch(device, args: argparse.Namespace):
    errors: list[str] = []

    if args.process:
        try:
            session = device.attach(args.process)
            print(f"[yamada-dom] Đã attach process {args.process}.", file=sys.stderr)
            return session, False
        except Exception as exc:
            errors.append(f"attach process {args.process}: {exc}")

    pid = app_pid(device, args.bundle_id)
    if pid:
        try:
            session = device.attach(pid)
            print(f"[yamada-dom] Đã attach app {args.bundle_id} pid={pid}.", file=sys.stderr)
            return session, False
        except Exception as exc:
            errors.append(f"attach app pid={pid}: {exc}")

    if args.bundle_id:
        try:
            pid = device.spawn([args.bundle_id])
            session = device.attach(pid)
            device.resume(pid)
            print(f"[yamada-dom] Đã mở app {args.bundle_id} pid={pid}.", file=sys.stderr)
            time.sleep(max(0, float(args.launch_wait)))
            return session, True
        except Exception as exc:
            errors.append(f"spawn app {args.bundle_id}: {exc}")

    raise RuntimeError("Không attach/mở được Yamada app. " + " | ".join(errors))


def is_not_ready_start_screen(screen: Any) -> bool:
    if not isinstance(screen, dict):
        return True
    state = str(screen.get("state") or "")
    url = str(screen.get("url") or "")
    body = str(screen.get("bodyText") or "")
    if state in ("", "unknown", "no_webview_or_no_result"):
        return True
    return (not url or url == "about:blank") and not body.strip()


def wait_for_webview_content(ex, timeout_ms: int, poll_ms: int) -> Any:
    deadline = time.time() + max(0, timeout_ms) / 1000
    last = None
    while True:
        last = parse_result(ex.yamadascreen(0))
        if not is_not_ready_start_screen(last):
            return last
        if time.time() >= deadline:
            print("[yamada-dom] WebView vẫn chưa nhận diện được màn sau khi chờ app load.", file=sys.stderr)
            return last
        time.sleep(max(0.1, poll_ms / 1000))


def run_with_frida(args: argparse.Namespace) -> Any:
    frida = import_frida()
    if frida is None:
        raise RuntimeError("Current Python cannot import frida.")

    profile_js = Path(args.profile_js).expanduser()
    combined = load_combined_agent(profile_js)
    device = resolve_frida_device(frida, args.device_id, timeout=10)
    session, did_launch = attach_or_launch(device, args)
    try:
        script = session.create_script(combined)
        script.on("message", lambda message, data: print(message.get("stack") or message, file=sys.stderr) if message.get("type") == "error" else None)
        script.load()
        time.sleep(args.load_wait)
        ex = script.exports_sync
        if args.action == "run":
            wait_for_webview_content(ex, args.initial_wait_timeout_ms, args.poll_ms)

        if args.action == "screen":
            return parse_result(ex.yamadascreen(0))
        if args.action == "step":
            options = {"submit": not args.no_submit, "dryRun": args.dry_run}
            return parse_result(ex.yamadastep(0, "{}", json.dumps(options)))

        options = {
            "maxSteps": args.max_steps,
            "delayMs": args.delay_ms,
            "pollMs": args.poll_ms,
            "waitTimeoutMs": args.wait_timeout_ms,
            "stablePolls": args.stable_polls,
            "includeWaits": args.include_waits,
            "submit": not args.no_submit,
            "dryRun": args.dry_run,
        }
        return parse_result(ex.yamadarun(0, "{}", json.dumps(options)))
    finally:
        session.detach()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Attach to Yamada and run the Frida DOM registration agent.")
    parser.add_argument("--process", default="yamadadenki")
    parser.add_argument("--bundle-id", default=os.environ.get("YAMADA_APP_ID", DEFAULT_APP_ID))
    parser.add_argument("--device-id", default=os.environ.get("FRIDA_DEVICE_ID", "auto"))
    parser.add_argument("--profile-js", default=str(ROOT_DIR / "agents" / "current_profile.js"))
    parser.add_argument("--action", choices=["run", "step", "screen"], default="run")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--delay-ms", type=int, default=300)
    parser.add_argument("--poll-ms", type=int, default=500)
    parser.add_argument("--wait-timeout-ms", type=int, default=15000)
    parser.add_argument("--stable-polls", type=int, default=2)
    parser.add_argument("--include-waits", action="store_true")
    parser.add_argument("--load-wait", type=float, default=0.5)
    parser.add_argument("--launch-wait", type=float, default=5.0)
    parser.add_argument("--initial-wait-timeout-ms", type=int, default=15000)
    parser.add_argument("--no-submit", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if import_frida() is None and not args._child:
            python_bin = find_frida_python()
            cmd = [python_bin, str(Path(__file__).resolve()), *sys.argv[1:], "--_child"]
            completed = subprocess.run(cmd, text=True, capture_output=True, check=False)
            if completed.stdout:
                print(completed.stdout, end="")
            if completed.stderr:
                print(completed.stderr, end="", file=sys.stderr)
            return completed.returncode

        result = run_with_frida(args)
        json_print(result)
        return 0
    except Exception as exc:
        print(f"[yamada-dom] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
