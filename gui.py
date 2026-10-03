"""Tkinter UI tối giản cho Yamada_Reg.

UI này cố ý đi theo kiểu `Neppi_Pay/gui.py`: desktop Tkinter/ttk, chạy tác vụ
ở background thread và đẩy output vào khung log.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_XLSX = Path.home() / "Downloads" / "Yamada_accounts.xlsx"


def _can_import(module: str, python_bin: str) -> bool:
    try:
        return subprocess.run(
            [python_bin, "-c", f"import {module}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
    except OSError:
        return False


def _bootstrap_tkinter() -> None:
    try:
        import tkinter  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    if __name__ != "__main__":
        return
    if os.environ.get("YAMADA_TK_BOOTSTRAPPED"):
        return
    candidates = [
        os.environ.get("YAMADA_TK_PYTHON", ""),
        "/usr/bin/python3",
        "/Applications/Xcode.app/Contents/Developer/usr/bin/python3",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists() and _can_import("tkinter", candidate):
            env = dict(os.environ)
            env["YAMADA_TK_BOOTSTRAPPED"] = "1"
            os.execve(candidate, [candidate, str(Path(__file__).resolve()), *sys.argv[1:]], env)


_bootstrap_tkinter()

import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox, ttk  # noqa: E402

import src.config as config  # noqa: E402


def _find_runtime_python() -> str:
    candidates = [
        os.environ.get("YAMADA_RUNTIME_PYTHON", ""),
        "/opt/homebrew/bin/python3",
        "/opt/homebrew/opt/python@3.14/bin/python3.14",
        sys.executable,
        "/usr/bin/python3",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists() and _can_import("openpyxl", candidate):
            return candidate
    return sys.executable


class YamadaRegGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Yamada Reg")
        self.geometry("940x640")
        self.minsize(780, 520)
        self.running = False
        self.stop_requested = False
        self.current_proc: subprocess.Popen | None = None
        self.proc_lock = threading.Lock()
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.runtime_python = _find_runtime_python()
        self.vars: dict[str, tk.Variable] = {}

        self._build()
        self.after(100, self._drain_logs)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _var(self, name: str, value, kind: str = "str"):
        cls = {"bool": tk.BooleanVar, "int": tk.IntVar}.get(kind, tk.StringVar)
        self.vars[name] = cls(value=value)
        return self.vars[name]

    def _build(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        settings = ttk.LabelFrame(root, text="Cấu hình chạy thử", padding=10)
        settings.pack(fill="x")

        xlsx_default = str(config.XLSX_PATH or (DEFAULT_XLSX if DEFAULT_XLSX.exists() else ""))
        self._path_row(settings, 0, "Excel", "xlsx_path", xlsx_default, self.choose_xlsx)

        ttk.Label(settings, text="Sheet").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(
            settings,
            values=["Outlooks", "Gmails", "Iclouds"],
            textvariable=self._var("active_sheet", config.ACTIVE_SHEET or "Iclouds"),
            width=18,
        ).grid(row=1, column=1, sticky="w")
        ttk.Label(settings, text="Số nick").grid(row=1, column=2, sticky="e", padx=(20, 6))
        ttk.Entry(settings, textvariable=self._var("run_limit", "0"), width=8).grid(row=1, column=3, sticky="w")
        self._var("no_reload", False, "bool")

        self._var("runtime_python", self.runtime_python)

        self._var("no_submit", False, "bool")
        ttk.Label(settings, text="Wait màn (s)").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Spinbox(
            settings,
            from_=5,
            to=60,
            textvariable=self._var("wait_seconds", 15, "int"),
            width=8,
        ).grid(row=2, column=1, sticky="w", pady=4)
        ttk.Label(settings, text="Thiết bị").grid(row=2, column=2, sticky="e", padx=(20, 6))
        self.device_combo = ttk.Combobox(
            settings,
            values=["auto", "all"],
            textvariable=self._var("device_id", str(getattr(config, "_cfg", {}).get("device_id", "auto") or "auto")),
            width=34,
        )
        self.device_combo.grid(row=2, column=3, columnspan=2, sticky="ew", pady=4)
        ttk.Button(settings, text="Làm mới", command=self.refresh_devices).grid(row=2, column=5, sticky="ew", padx=(6, 0))

        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(4, weight=1)

        actions = ttk.Frame(root)
        actions.pack(fill="x", pady=8)
        ttk.Button(actions, text="Chạy", command=self.run_row).pack(side="left")
        self.stop_button = ttk.Button(actions, text="Dừng", command=self.stop_current, state="disabled")
        self.stop_button.pack(side="left", padx=6)
        ttk.Button(actions, text="Lưu cấu hình", command=self.save_settings).pack(side="right")

        self.status = ttk.Label(root, text="Sẵn sàng")
        self.status.pack(anchor="w")

        log_frame = ttk.LabelFrame(root, text="Log", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(8, 0))

        log_actions = ttk.Frame(log_frame)
        log_actions.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Button(log_actions, text="Xóa log", command=self.clear_log).pack(side="left")
        ttk.Button(log_actions, text="Sao chép log", command=self.copy_log).pack(side="left", padx=6)

        body = ttk.Frame(log_frame)
        body.pack(side="top", fill="both", expand=True)
        self.log_text = tk.Text(body, wrap="word", state="disabled")
        scroll = ttk.Scrollbar(body, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self._log("Sẵn sàng. Số nick = 0 để chạy full sheet, hoặc nhập N để chạy N nick đầu.")
        self.after(300, self.refresh_devices)

    def _path_row(self, parent, row, label, name, value, command) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self._var(name, value)).grid(
            row=row, column=1, columnspan=4, sticky="ew"
        )
        ttk.Button(parent, text="Chọn", command=command).grid(row=row, column=5, sticky="ew", padx=(6, 0))

    def choose_xlsx(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Excel", "*.xlsx")])
        if path:
            self.vars["xlsx_path"].set(path)

    def choose_python(self) -> None:
        path = filedialog.askopenfilename()
        if path:
            self.vars["runtime_python"].set(path)
            self.runtime_python = path

    def save_settings(self) -> None:
        data = dict(getattr(config, "_cfg", {}))
        data["xlsx_path"] = self.vars["xlsx_path"].get().strip()
        data["active_sheet"] = self.vars["active_sheet"].get().strip() or "Iclouds"
        data["device_id"] = self._selected_device_id()
        config.CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        config.XLSX_PATH = data["xlsx_path"]
        config.ACTIVE_SHEET = data["active_sheet"]
        self._log(f"Đã lưu cấu hình vào {config.CONFIG_FILE}")
        messagebox.showinfo("Yamada Reg", "Đã lưu cấu hình.")

    def _row_args(self) -> list[str]:
        xlsx = self.vars["xlsx_path"].get().strip()
        sheet = self.vars["active_sheet"].get().strip() or "Iclouds"
        row = self.vars.get("row", tk.StringVar(value="2")).get().strip()
        if not xlsx:
            raise RuntimeError("Chưa chọn file Excel.")
        if not row.isdigit():
            raise RuntimeError("Row phải là số.")
        return ["--xlsx", xlsx, "--sheet", sheet, "--row", row]

    def _batch_args(self) -> list[str]:
        xlsx = self.vars["xlsx_path"].get().strip()
        sheet = self.vars["active_sheet"].get().strip() or "Iclouds"
        limit = self.vars["run_limit"].get().strip()
        if not xlsx:
            raise RuntimeError("Chưa chọn file Excel.")
        if not limit.isdigit():
            raise RuntimeError("Số nick phải là số, 0 nghĩa là chạy full sheet.")
        return ["--xlsx", xlsx, "--sheet", sheet, "--limit", limit, "--device-id", self._selected_device_id()]

    def _selected_device_id(self) -> str:
        value = self.vars["device_id"].get().strip() or "auto"
        if " | " in value:
            return value.split(" | ", 1)[0].strip()
        return value

    def _single_device_id(self) -> str:
        value = self._selected_device_id()
        if value == "all" or "," in value:
            return "auto"
        return value

    def refresh_devices(self) -> None:
        def worker():
            values = ["auto", "all"]
            try:
                completed = subprocess.run(
                    [self._py(), "scripts/frida_devices.py"],
                    cwd=str(ROOT_DIR),
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=15,
                )
                if completed.returncode != 0:
                    raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "Không liệt kê được device.")
                payload = json.loads(completed.stdout or "[]")
                if isinstance(payload, dict) and payload.get("error"):
                    raise RuntimeError(str(payload.get("error")))
                for device in payload if isinstance(payload, list) else []:
                    device_id = str(device.get("id") or "").strip()
                    name = str(device.get("name") or "iPhone").strip()
                    if device_id:
                        values.append(f"{device_id} | {name}")
                self.after(0, lambda: self._apply_device_values(values))
            except Exception as exc:
                self._log(f"[device] Không refresh được Frida devices: {exc}")
                self.after(0, lambda: self._apply_device_values(values))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_device_values(self, values: list[str]) -> None:
        current = self.vars["device_id"].get().strip() or "auto"
        self.device_combo.configure(values=values)
        current_id = current.split(" | ", 1)[0].strip()
        for value in values:
            if value == current or value.split(" | ", 1)[0].strip() == current_id:
                self.vars["device_id"].set(value)
                break
        else:
            self.vars["device_id"].set(values[0])
        self._log(f"[device] {len(values) - 2} thiết bị USB Frida. Chọn 'all' để chạy song song.")

    def _py(self) -> str:
        return self.vars["runtime_python"].get().strip() or self.runtime_python

    def crane_info(self) -> None:
        self._run_commands("Xem Crane", [[self._py(), "scripts/crane_container_manager.py", "info", "--device-id", self._single_device_id()]])

    def _profile_command(self, row_args: list[str]) -> list[str]:
        return [
            self._py(),
            "scripts/yamada_profile_from_excel.py",
            *row_args,
            "--out",
            str(ROOT_DIR / "agents" / "current_profile.js"),
        ]

    def _prepare_commands(self, row_args: list[str]) -> list[list[str]]:
        crane_cmd = [self._py(), "scripts/crane_container_manager.py", "ensure-row", *row_args]
        crane_cmd.extend(["--device-id", self._single_device_id(), "--container-mode", "create"])
        if self.vars["no_reload"].get():
            crane_cmd.append("--no-reload")
        return [crane_cmd, self._profile_command(row_args)]

    def _dom_command(self, action: str) -> list[str]:
        wait_ms = max(5, int(self.vars["wait_seconds"].get() or 15)) * 1000
        cmd = [
            self._py(),
            "scripts/yamada_dom_runner.py",
            "--action",
            action,
            "--wait-timeout-ms",
            str(wait_ms),
            "--device-id",
            self._single_device_id(),
        ]
        if self.vars["no_submit"].get():
            cmd.append("--no-submit")
        return cmd

    def prepare_row(self) -> None:
        try:
            row_args = self._row_args()
        except Exception as exc:
            messagebox.showerror("Thiếu dữ liệu", str(exc))
            return
        self._run_commands("Chuẩn bị container + profile", self._prepare_commands(row_args))

    def run_row(self) -> None:
        try:
            batch_args = self._batch_args()
        except Exception as exc:
            messagebox.showerror("Thiếu dữ liệu", str(exc))
            return
        wait_ms = max(5, int(self.vars["wait_seconds"].get() or 15)) * 1000
        cmd = [
            self._py(),
            "scripts/yamada_batch_flow.py",
            *batch_args,
            "--wait-timeout-ms",
            str(wait_ms),
        ]
        if self.vars["no_reload"].get():
            cmd.append("--no-reload")
        if self.vars["no_submit"].get():
            cmd.append("--no-submit")
        commands = [cmd]
        self._run_commands("Chạy batch", commands)

    def fetch_otp(self) -> None:
        try:
            row_args = self._row_args()
        except Exception as exc:
            messagebox.showerror("Thiếu dữ liệu", str(exc))
            return
        self._run_commands("Lấy OTP email", [self._fetch_otp_command(row_args)])

    def _fetch_otp_command(self, row_args: list[str]) -> list[str]:
        return [
            self._py(),
            "scripts/fetch_yamada_email_otp.py",
            *row_args,
            "--profile-js",
            str(ROOT_DIR / "agents" / "current_profile.js"),
        ]

    def fetch_otp_and_continue(self) -> None:
        try:
            row_args = self._row_args()
        except Exception as exc:
            messagebox.showerror("Thiếu dữ liệu", str(exc))
            return
        self._run_commands("Lấy OTP & chạy tiếp", [self._fetch_otp_command(row_args), self._dom_command("run")])

    def run_dom(self, action: str) -> None:
        commands = []
        if action in ("run", "step"):
            try:
                row_args = self._row_args()
            except Exception as exc:
                messagebox.showerror("Thiếu dữ liệu", str(exc))
                return
            commands.append(self._profile_command(row_args))
        commands.append(self._dom_command(action))
        self._run_commands(f"DOM {action}", commands)

    def _run_commands(self, title: str, commands: list[list[str]]) -> None:
        if self.running:
            messagebox.showinfo("Đang chạy", "Đang có tác vụ chạy, đợi xong rồi bấm tiếp.")
            return
        self.running = True
        self.stop_requested = False
        self.status.configure(text=f"Đang chạy: {title}")
        self.stop_button.configure(state="normal")
        threading.Thread(target=self._worker, args=(title, commands), daemon=True).start()

    def stop_current(self) -> None:
        self.stop_requested = True
        self._log("Đang dừng tác vụ...")
        self.status.configure(text="Đang dừng...")
        with self.proc_lock:
            proc = self.current_proc
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception:
                proc.terminate()

    def _worker(self, title: str, commands: list[list[str]]) -> None:
        try:
            self._log(f"\n=== {title} ===")
            for cmd in commands:
                if self.stop_requested:
                    raise RuntimeError("Đã dừng bởi người dùng.")
                self._log("$ " + " ".join(self._quote(part) for part in cmd))
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(ROOT_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    preexec_fn=os.setsid if hasattr(os, "setsid") else None,
                )
                with self.proc_lock:
                    self.current_proc = proc
                assert proc.stdout is not None
                for line in proc.stdout:
                    self._log(line.rstrip())
                    if self.stop_requested and proc.poll() is None:
                        try:
                            os.killpg(proc.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        except Exception:
                            proc.terminate()
                code = proc.wait()
                with self.proc_lock:
                    self.current_proc = None
                if self.stop_requested:
                    raise RuntimeError("Đã dừng bởi người dùng.")
                if code != 0:
                    raise RuntimeError(f"Lệnh lỗi exit={code}: {' '.join(cmd)}")
            self._log(f"=== {title}: xong ===")
            self.after(0, lambda: self.status.configure(text="Xong"))
        except Exception as exc:
            self._log(f"Lỗi: {exc}")
            self.after(0, lambda err=str(exc): self.status.configure(text=f"Lỗi: {err}"))
            self.after(0, lambda err=str(exc): messagebox.showerror(title, err))
        finally:
            with self.proc_lock:
                self.current_proc = None
            self.running = False
            self.stop_requested = False
            self.after(0, lambda: self.stop_button.configure(state="disabled"))

    def _quote(self, value: str) -> str:
        return "'" + value.replace("'", "'\\''") + "'" if any(ch.isspace() for ch in value) else value

    def _log(self, text: str) -> None:
        self.log_queue.put(text)

    def _drain_logs(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", line + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._drain_logs)

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def copy_log(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.log_text.get("1.0", "end-1c"))

    def on_close(self) -> None:
        self.destroy()


def main() -> None:
    YamadaRegGUI().mainloop()


if __name__ == "__main__":
    main()
