from __future__ import annotations

import csv
import threading
from datetime import datetime
from pathlib import Path

from src.utils.logger import get_logger


log = get_logger("csv_writer")


class ResultWriter:
    COLUMNS = [
        "email",
        "password",
        "phone",
        "proxy_used",
        "proxy_id",
        "status",
        "created_at",
        "error_details",
    ]

    def __init__(self, output_file: str, columns: list[str] | None = None):
        self.output_file = Path(output_file)
        self.columns = list(columns or self.COLUMNS)
        self.lock = threading.Lock()
        self.init_csv()

    def init_csv(self) -> None:
        with self.lock:
            self.output_file.parent.mkdir(parents=True, exist_ok=True)
            if not self.output_file.exists():
                with open(self.output_file, "w", newline="", encoding="utf-8-sig") as f:
                    csv.writer(f).writerow(self.columns)
                log.info("Đã khởi tạo CSV kết quả: %s", self.output_file)
                return

            with open(self.output_file, "r", newline="", encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))
            if not rows:
                rows = [self.columns]
            header = rows[0]
            missing = [col for col in self.columns if col not in header]
            if missing:
                header.extend(missing)
                for row in rows[1:]:
                    row.extend([""] * len(missing))
                with open(self.output_file, "w", newline="", encoding="utf-8-sig") as f:
                    csv.writer(f).writerows(rows)
                log.info("Đã bổ sung cột CSV còn thiếu: %s", ", ".join(missing))

    def _make_row(self, data: dict, header: list[str]) -> list[str]:
        payload = dict(data)
        if payload.get("status") == "SUCCESS" and not payload.get("created_at"):
            payload["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return [str(payload.get(col, "") or "") for col in header]

    def write(self, data: dict) -> None:
        email = str(data.get("email") or "").strip()
        if not email:
            return

        with self.lock:
            rows = []
            if self.output_file.exists():
                with open(self.output_file, "r", newline="", encoding="utf-8-sig") as f:
                    rows = list(csv.reader(f))
            if not rows:
                rows = [self.columns]

            header = rows[0]
            extra_cols = [key for key in data if key not in header]
            if extra_cols:
                header.extend(extra_cols)
                for row in rows[1:]:
                    row.extend([""] * len(extra_cols))

            new_row = self._make_row(data, header)
            found = False
            for idx, row in enumerate(rows[1:], start=1):
                if row and row[0].strip().lower() == email.lower():
                    rows[idx] = new_row
                    found = True
                    break
            if not found:
                rows.append(new_row)

            with open(self.output_file, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerows(rows)

        log.info("%s CSV: %s -> %s", "Cập nhật" if found else "Thêm mới", email, data.get("status", ""))
