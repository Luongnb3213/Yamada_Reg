from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


DEFAULT_FRIDA_PYTHON = "/Users/macbook/Library/Application Support/pipx/venvs/frida-tools/bin/python"


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
    candidates = [
        os.environ.get("FRIDA_PYTHON", ""),
        DEFAULT_FRIDA_PYTHON,
        sys.executable,
        "python3",
    ]
    for candidate in candidates:
        if candidate and (candidate == "python3" or Path(candidate).exists()) and can_import_frida(candidate):
            return candidate
    raise RuntimeError("Không tìm thấy Python có module frida.")


def main() -> int:
    code = """
import frida, json
print(json.dumps([
  {"id": d.id, "name": d.name, "type": d.type}
  for d in frida.enumerate_devices()
  if d.type == "usb"
], ensure_ascii=False))
"""
    try:
        completed = subprocess.run([find_frida_python(), "-c", code], text=True, capture_output=True, check=False)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "frida enumerate failed")
        print(completed.stdout.strip() or "[]")
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
