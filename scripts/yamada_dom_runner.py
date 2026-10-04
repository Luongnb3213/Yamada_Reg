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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spawn_throttle import spawn_slot


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


def wait_app_stopped(device, bundle_id: str, timeout_sec: float, poll_sec: float = 0.25) -> bool:
    deadline = time.time() + max(0, timeout_sec)
    while time.time() < deadline:
        if not app_pid(device, bundle_id):
            return True
        time.sleep(max(0.05, poll_sec))
    return not app_pid(device, bundle_id)


def springboard_pid(device) -> int | None:
    try:
        procs = device.enumerate_processes()
    except Exception:
        return None
    for proc in procs:
        if getattr(proc, "name", "") == "SpringBoard":
            pid = int(getattr(proc, "pid", 0) or 0)
            return pid if pid > 0 else None
    return None


WAKE_JS = r"""
rpc.exports = {
  wake: function () {
    var out = [];
    // Bật màn (unblank). BKSDisplayServicesSetScreenBlanked(0) = ON, (1) = OFF.
    try {
      var p = Module.getGlobalExportByName('BKSDisplayServicesSetScreenBlanked');
      if (p) { new NativeFunction(p, 'void', ['uint32'])(0); out.push('unblank'); }
      else out.push('no_bks');
    } catch (e) { out.push('bks_err:' + e); }
    // Declare user activity: đánh thức + reset idle timer (giúp WiFi/network về nhanh).
    try {
      var dp = Module.getGlobalExportByName('IOPMAssertionDeclareUserActivity');
      var cs = Module.getGlobalExportByName('CFStringCreateWithCString');
      if (dp && cs) {
        var name = new NativeFunction(cs, 'pointer', ['pointer', 'pointer', 'uint32'])(
          ptr(0), Memory.allocUtf8String('yamada-wake'), 0x08000100);
        var idOut = Memory.alloc(4);
        new NativeFunction(dp, 'int', ['pointer', 'int', 'pointer'])(name, 0, idOut);
        out.push('user_activity');
      } else out.push('no_iopm');
    } catch (e) { out.push('iopm_err:' + e); }
    return out.join(',');
  },
  // Đo WiFi/internet đã về chưa, kiểm tra NGAY trong app (đại diện thật nhất).
  // Trả 'up:<flags>' khi reachable và không cần dial, 'down:<flags>' khi chưa.
  reachable: function (host) {
    try {
      var create = Module.getGlobalExportByName('SCNetworkReachabilityCreateWithName');
      var getFlags = Module.getGlobalExportByName('SCNetworkReachabilityGetFlags');
      if (!create || !getFlags) return 'no_sc';
      var ref = new NativeFunction(create, 'pointer', ['pointer', 'pointer'])(
        ptr(0), Memory.allocUtf8String(host));
      if (ref.isNull()) return 'null_ref';
      var flagsPtr = Memory.alloc(4);
      var ok = new NativeFunction(getFlags, 'int', ['pointer', 'pointer'])(ref, flagsPtr);
      if (!ok) return 'getflags_fail';
      var flags = flagsPtr.readU32();
      var kReachable = 1 << 1;      // kSCNetworkReachabilityFlagsReachable
      var kConnReq = 1 << 2;        // kSCNetworkReachabilityFlagsConnectionRequired
      var up = ((flags & kReachable) !== 0) && ((flags & kConnReq) === 0);
      return (up ? 'up:' : 'down:') + flags;
    } catch (e) { return 'err:' + e; }
  }
};
"""

# Đọc áp lực RAM của MÁY (không phải của process): host_statistics64(HOST_VM_INFO64)
# trả vm_statistics64 -> free_count + compressor_page_count, đúng hai số kernel dùng để
# quyết định jetsam/panic. host-level nên chỉ cần 1 process bất kỳ để gọi (spawn frozen).
MEMINFO_JS = r"""
rpc.exports = {
  meminfo: function () {
    try {
      var PAGE = 16384; // iPhone 7 / iOS 15 = 16KB/page (đã xác nhận trong JetsamEvent)
      var hostSelf = new NativeFunction(Module.getGlobalExportByName('mach_host_self'), 'uint32', [])();
      var hs64 = new NativeFunction(Module.getGlobalExportByName('host_statistics64'), 'int',
        ['uint32', 'int', 'pointer', 'pointer']);
      var HOST_VM_INFO64 = 4;
      var COUNT = 38;                 // sizeof(vm_statistics64_data_t)/sizeof(integer_t)
      var info = Memory.alloc(COUNT * 4);
      var cnt = Memory.alloc(4); cnt.writeU32(COUNT);
      var kr = hs64(hostSelf, HOST_VM_INFO64, info, cnt);
      if (kr !== 0) return 'err:kr=' + kr;
      var free = info.add(0).readU32();     // free_count   (offset 0)
      var comp = info.add(128).readU32();   // compressor_page_count (offset 128)
      return 'ok:free=' + free + ',comp=' + comp + ',ps=' + PAGE;
    } catch (e) { return 'err:' + e; }
  }
};
"""

# Bật đo WiFi-về-sau-respring bằng cách touch file cờ này (self-limiting: xoá đi là tắt).
MEASURE_FLAG = Path.home() / ".yamada_respring_measure"
MEASURE_LOG = Path(__file__).resolve().parent.parent / "logs" / "respring_wifi_measure.log"
MEASURE_HOST = "www.apple.com"
MEASURE_TIMEOUT = 90.0


def _log_measure(line: str) -> None:
    try:
        MEASURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with MEASURE_LOG.open("a") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def wake_screen(device, bundle_id: str, t0: float | None = None, device_id: str = "",
                wait_network: bool = False, net_timeout: float = 60.0) -> dict:
    """Bật màn + declare user activity sau respring. Spawn app frozen để có process
    link BackBoardServices/IOKit, gọi native rồi kill lại. KHÔNG đụng SpringBoard.

    wait_network=True: NETWORK GATE — poll reachability NGAY trong app cho tới khi
    internet thật sự về (hoặc hết net_timeout) rồi mới trả về, để batch không giao
    row kế cho máy đang mất mạng (tránh app nhảy vào trang エラー/簡易モード).
    Nếu MEASURE_FLAG tồn tại và có t0: ghi thêm thời gian mạng về ra measure log.

    Trả dict: {wake, net_back_sec, net_last}."""
    out = {"wake": "skip_no_bundle", "net_back_sec": None, "net_last": ""}
    if not bundle_id:
        return out
    measure = t0 is not None and MEASURE_FLAG.exists()
    pid = None
    try:
        pid = device.spawn([bundle_id])
        session = device.attach(pid)
        script = session.create_script(WAKE_JS)
        script.load()
        out["wake"] = script.exports_sync.wake()
        if wait_network or measure:
            base = t0 if t0 is not None else time.time()
            recovered = None
            last = ""
            deadline = time.time() + float(net_timeout)
            while time.time() < deadline:
                try:
                    last = script.exports_sync.reachable(MEASURE_HOST)
                except Exception as exc:
                    last = f"rpc_err:{exc}"
                if last.startswith("up:"):
                    recovered = time.time() - base
                    break
                if last.startswith(("no_sc", "null_ref", "getflags_fail")):
                    break  # probe không chạy được -> đừng quay vô ích, nhả theo cooldown
                time.sleep(1.0)
            out["net_back_sec"] = recovered
            out["net_last"] = last
            if recovered is not None:
                print(f"[yamada-dom] Mạng đã về sau {recovered:.1f}s (từ lúc kill SpringBoard).", file=sys.stderr)
            else:
                print(f"[yamada-dom] Chưa xác nhận mạng về trong {net_timeout:.0f}s (last={last}).", file=sys.stderr)
            if measure:
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                if recovered is not None:
                    _log_measure(f"{stamp}\t{device_id[:8]}\tWIFI_BACK\t{recovered:.1f}s\tlast={last}")
                else:
                    _log_measure(f"{stamp}\t{device_id[:8]}\tTIMEOUT_OR_NOPROBE\t>{net_timeout:.0f}s\tlast={last}")
        try:
            session.detach()
        except Exception:
            pass
        return out
    except Exception as exc:
        out["wake"] = f"lỗi: {exc}"
        return out
    finally:
        if pid:
            try:
                device.kill(pid)
            except Exception:
                pass


def read_meminfo(device, bundle_id: str = "") -> dict:
    """Đọc áp lực RAM của MÁY bằng host_statistics64 (mach host-level -> chạy được
    trong BẤT KỲ tiến trình nào, không cần app Yamada).

    KHÔNG spawn app nữa: lúc batch chạy, mỗi máy đang spawn/drive chính app Yamada
    cho từng row, nên spawn thêm một instance Yamada thứ hai va chạm -> lỗi 100%
    (đã thấy 81/81 lần trong log), lại nặng + giựt USB trên máy 2GB.
    Thay vào đó ATTACH đọc-only vào SpringBoard (luôn sống) rồi detach ngay: không
    gây safe mode, không mất jailbreak, rất nhẹ. bundle_id giữ lại cho tương thích
    chữ ký lệnh gọi nhưng không dùng tới."""
    out = {"ok": False, "action": "meminfo", "free_mb": None, "compressor_mb": None, "raw": ""}
    pid = springboard_pid(device)
    if not pid:
        out["raw"] = "no_springboard"
        return out
    session = None
    try:
        session = device.attach(pid)
        script = session.create_script(MEMINFO_JS)
        script.load()
        raw = script.exports_sync.meminfo()
        out["raw"] = raw
        if isinstance(raw, str) and raw.startswith("ok:"):
            fields = dict(p.split("=", 1) for p in raw[3:].split(",") if "=" in p)
            ps = int(fields.get("ps", "16384"))
            out["ok"] = True
            out["free_mb"] = round(int(fields.get("free", "0")) * ps / 1048576, 1)
            out["compressor_mb"] = round(int(fields.get("comp", "0")) * ps / 1048576, 1)
        return out
    except Exception as exc:
        out["raw"] = f"lỗi: {exc}"
        return out
    finally:
        if session:
            try:
                session.detach()
            except Exception:
                pass


def respring_springboard(device, wait_sec: float, bundle_id: str = "", net_timeout: float = 60.0) -> dict:
    """Respring = kill SpringBoard (launchd tự dựng lại). Chỉ device.kill, KHÔNG
    attach/inject SpringBoard nên KHÔNG gây safe mode và KHÔNG mất jailbreak (khác
    hẳn panic reboot). Dùng để xả RAM/compressor định kỳ giữa các row.
    Sau respring iOS về màn khóa + TẮT màn + rớt WiFi một lúc -> wake lại màn +
    declare user activity, rồi NETWORK GATE: chờ tới khi internet thật sự về mới trả
    về, để batch không giao row kế cho máy đang mất mạng (tránh app vào trang エラー)."""
    pid = springboard_pid(device)
    if not pid:
        return {"ok": False, "action": "respring", "reason": "không thấy process SpringBoard"}
    t0 = time.time()  # mốc: lúc kill SpringBoard (dùng cho gate + đo)
    try:
        device.kill(pid)
    except Exception as exc:
        return {"ok": False, "action": "respring", "reason": str(exc)}
    print(f"[yamada-dom] Đã respring (kill SpringBoard pid={pid}); chờ {wait_sec:.0f}s cho SpringBoard dựng lại.", file=sys.stderr)
    time.sleep(max(0.0, float(wait_sec)))

    # Chờ SpringBoard quay lại (cần cho FBS spawn ở bước wake) tối đa thêm 20s.
    new_sb = None
    wait_deadline = time.time() + 20.0
    while time.time() < wait_deadline:
        new_sb = springboard_pid(device)
        if new_sb and new_sb != pid:
            break
        time.sleep(1.0)

    device_id = getattr(device, "id", "") or ""
    if new_sb:
        wake = wake_screen(device, bundle_id, t0=t0, device_id=device_id,
                           wait_network=True, net_timeout=net_timeout)
    else:
        wake = {"wake": "skip_no_springboard", "net_back_sec": None, "net_last": ""}
    print(f"[yamada-dom] Wake + network gate sau respring: {wake}", file=sys.stderr)
    net_ok = wake.get("net_back_sec") is not None
    return {"ok": True, "action": "respring", "springboard_pid": pid,
            "springboard_new_pid": new_sb, "wake": wake.get("wake"),
            "net_back_sec": wake.get("net_back_sec"), "net_ok": net_ok}


def kill_running_app(device, bundle_id: str, timeout_sec: float, ensure: bool = False) -> dict:
    """Kill app nếu đang chạy. Trả về {stopped, killed_pid}.

    ensure=True dùng cho kill cuối row: app có thể vừa được spawn bởi
    launch_app_after_registration và chưa kịp xuất hiện trong enumerate_applications
    (iOS báo pid=0 lúc đang khởi động). Nếu chưa thấy pid thì chờ ngắn cho pid hiện
    ra rồi mới kill, tránh kill no-op để app khởi động xong rồi đứng chết trên màn.
    ensure=False giữ nguyên hành vi cũ (không thấy pid = coi như đã tắt)."""
    out = {"stopped": True, "killed_pid": None}
    pid = app_pid(device, bundle_id)
    if not pid and ensure:
        appear_deadline = time.time() + min(max(0.0, timeout_sec), 4.0)
        while time.time() < appear_deadline:
            time.sleep(0.25)
            pid = app_pid(device, bundle_id)
            if pid:
                break
    if not pid:
        return out
    try:
        device.kill(pid)
        print(f"[yamada-dom] Đã kill app {bundle_id} pid={pid}.", file=sys.stderr)
    except Exception as exc:
        print(f"[yamada-dom] Kill app {bundle_id} pid={pid} lỗi tạm: {exc}", file=sys.stderr)
    out["killed_pid"] = pid
    out["stopped"] = wait_app_stopped(device, bundle_id, timeout_sec)
    if not out["stopped"]:
        print(f"[yamada-dom] App {bundle_id} vẫn còn pid sau khi chờ tắt.", file=sys.stderr)
    return out


def spawn_attach_resume(device, args: argparse.Namespace, errors: list[str]):
    last_exc: Exception | None = None
    attempts = max(1, int(args.spawn_attempts))
    backoff = max(0.0, float(args.spawn_backoff_sec))
    for attempt in range(1, attempts + 1):
        pid = None
        try:
            with spawn_slot(slots=args.spawn_slots, label=f"dom {args.bundle_id}"):
                pid = device.spawn([args.bundle_id])
                session = device.attach(pid)
                device.resume(pid)
            print(f"[yamada-dom] Đã mở app {args.bundle_id} pid={pid}.", file=sys.stderr)
            time.sleep(max(0, float(args.launch_wait)))
            return session, True
        except Exception as exc:
            last_exc = exc
            errors.append(f"spawn app {args.bundle_id} attempt {attempt}/{attempts}: {exc}")
            if pid:
                try:
                    device.kill(pid)
                except Exception:
                    pass
            if attempt < attempts:
                print(
                    f"[yamada-dom] Spawn app lỗi tạm ({attempt}/{attempts}): {exc}; chờ {backoff:.1f}s rồi thử lại.",
                    file=sys.stderr,
                )
                time.sleep(backoff)
    if last_exc:
        raise last_exc
    raise RuntimeError("spawn failed")


def attach_or_launch(device, args: argparse.Namespace):
    errors: list[str] = []

    if args.fresh_launch and args.bundle_id:
        kill_running_app(device, args.bundle_id, args.kill_wait_sec)
        try:
            return spawn_attach_resume(device, args, errors)
        except Exception as exc:
            errors.append(f"fresh spawn app {args.bundle_id}: {exc}")
            raise RuntimeError("Không attach/mở được Yamada app. " + " | ".join(errors))

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
            return spawn_attach_resume(device, args, errors)
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

    device = resolve_frida_device(frida, args.device_id, timeout=10)

    # Dọn RAM giữa/cuối row: kill app hoặc respring. Không load profile, không spawn app.
    if args.action == "kill":
        res = kill_running_app(device, args.bundle_id, args.kill_wait_sec, ensure=args.ensure_killed)
        return {"ok": bool(res["stopped"]), "action": "kill", "bundle_id": args.bundle_id,
                "stopped": res["stopped"], "killed_pid": res["killed_pid"]}
    if args.action == "respring":
        return respring_springboard(device, args.respring_wait, args.bundle_id,
                                    net_timeout=args.respring_net_timeout)
    if args.action == "meminfo":
        return read_meminfo(device, args.bundle_id)

    profile_js = Path(args.profile_js).expanduser()
    combined = load_combined_agent(profile_js)
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
    parser.add_argument("--process", default="")
    parser.add_argument("--bundle-id", default=os.environ.get("YAMADA_APP_ID", DEFAULT_APP_ID))
    parser.add_argument("--device-id", default=os.environ.get("FRIDA_DEVICE_ID", "auto"))
    parser.add_argument("--profile-js", default=str(ROOT_DIR / "agents" / "current_profile.js"))
    parser.add_argument("--action", choices=["run", "step", "screen", "kill", "respring", "meminfo"], default="run")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--delay-ms", type=int, default=300)
    parser.add_argument("--poll-ms", type=int, default=500)
    parser.add_argument("--wait-timeout-ms", type=int, default=15000)
    parser.add_argument("--stable-polls", type=int, default=2)
    parser.add_argument("--include-waits", action="store_true")
    parser.add_argument("--load-wait", type=float, default=0.5)
    parser.add_argument("--launch-wait", type=float, default=5.0)
    parser.add_argument("--initial-wait-timeout-ms", type=int, default=15000)
    parser.add_argument("--fresh-launch", action="store_true", help="Kill the app first, then spawn it fresh for a new Crane container.")
    parser.add_argument("--spawn-attempts", type=int, default=4)
    parser.add_argument("--spawn-backoff-sec", type=float, default=1.5)
    parser.add_argument(
        "--spawn-slots",
        type=int,
        default=None,
        help="Số máy tối đa được spawn cùng lúc (mặc định lấy env YAMADA_SPAWN_SLOTS=3).",
    )
    parser.add_argument("--kill-wait-sec", type=float, default=5.0)
    parser.add_argument(
        "--ensure-killed",
        action="store_true",
        help="action kill: chờ pid hiện ra rồi mới kill và xác nhận app đã tắt (cho kill cuối row, tránh race với launch_app_after_registration).",
    )
    parser.add_argument("--respring-wait", type=float, default=8.0, help="Chờ bao nhiêu giây sau khi kill SpringBoard cho UI dựng lại (action respring).")
    parser.add_argument("--respring-net-timeout", type=float, default=60.0, help="NETWORK GATE: sau respring+wake, poll tới khi internet về mới trả về, tối đa bao nhiêu giây (0 hoặc probe fail -> bỏ qua, nhả theo cooldown). Mặc định 60.")
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
