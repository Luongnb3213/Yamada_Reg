from __future__ import annotations

import threading
from pathlib import Path
from src.utils.logger import get_logger
from src import config
from src.utils.proxy_health import proxy_group_key, proxy_group_label

log = get_logger("proxy_pool")

class ProxyPool:
    def __init__(self, proxy_list: list):
        self.proxies = []
        self.lock = threading.Lock()
        self.index = 0
        
        # Lưu các proxy index đã hoàn thành đăng ký đủ số accounts tối đa
        self.retired_indices = set()
        # Đánh dấu các proxy đã chết vĩnh viễn
        self.dead_indices = set()
        # Đếm số lần lỗi liên tiếp của toàn hệ thống proxy
        self.consecutive_failures = 0
        # Đếm số lần đã sử dụng cho mỗi proxy index (tính cả thành công lẫn thất bại)
        self.usage_counts = {} 
        # ĐẢM BẢO WORKER KHÔNG DÙNG CHUNG PROXY: Set các proxy index đang bị khóa bởi worker
        self.in_use_indices = set()
        # Proxy được claim ngay lúc cấp cho account; mark_used() sau đó phải idempotent.
        self.claimed_indices = set()
        self.group_failures = {}
        self.blocked_groups = set()
        self.circuit_open = False
        self.circuit_reason = ""
        
        self.load_from_list(proxy_list)

    def load_from_list(self, proxy_list: list):
        if not proxy_list:
            log.warning("Danh sách proxy trống. Chạy không dùng proxy.")
            return

        for position, item in enumerate(proxy_list, start=1):
            if isinstance(item, dict):
                raw = str(item.get("raw") or item.get("proxy") or "").strip()
                proxy_id = str(item.get("proxy_id") or f"PX-{position:06d}").strip()
                sheet_row = item.get("sheet_row")
            else:
                raw = str(item or "").strip()
                proxy_id = f"PX-{position:06d}"
                sheet_row = None

            parsed = self.parse_proxy_string(raw)
            if not parsed:
                continue
            # Mỗi dòng Excel là một proxy slot độc lập, kể cả khi chuỗi gateway
            # giống hệt dòng khác. Dùng proxy_id để cập nhật đúng một dòng.
            parsed["proxy_id"] = proxy_id
            parsed["sheet_row"] = sheet_row
            parsed["group_key"] = proxy_group_key(parsed)
            parsed["group_label"] = proxy_group_label(parsed)
            self.proxies.append(parsed)

        log.info(f"Loaded {len(self.proxies)} proxies vào ProxyPool")

    def load_permanent_counts(self, sheets_manager):
        """Đọc sheet Accounts để đếm số lần proxy đã được dùng (SUCCESS) vĩnh viễn.
        
        Gọi hàm này sau khi khởi tạo ProxyPool để khôi phục lịch sử sử dụng proxy.
        """
        if not sheets_manager or not sheets_manager.is_connected():
            return
        
        try:
            all_values = sheets_manager.accounts_sheet.get_all_values()
            if len(all_values) <= 1:
                return
            
            headers = all_values[0]
            try:
                proxy_idx_col = headers.index("Proxy Used")
                status_idx_col = headers.index("Status")
            except ValueError:
                log.warning("Không tìm thấy cột 'Proxy Used' hoặc 'Status' trong sheet Accounts")
                return
            
            # Đếm số lần mỗi proxy raw string đã dùng cho account SUCCESS
            proxy_success_map = {}
            for row in all_values[1:]:
                if len(row) > max(proxy_idx_col, status_idx_col):
                    proxy_raw = row[proxy_idx_col].strip()
                    status = row[status_idx_col].strip().upper()
                    if proxy_raw and status == "SUCCESS":
                        proxy_success_map[proxy_raw] = proxy_success_map.get(proxy_raw, 0) + 1
            
            # Map proxy raw string về proxy index trong pool
            with self.lock:
                for idx, proxy_dict in enumerate(self.proxies):
                    raw = proxy_dict.get("raw", "")
                    if raw in proxy_success_map:
                        count = proxy_success_map[raw]
                        self.usage_counts[idx] = count
                        log.info(f"   [Proxy Pool] Proxy index {idx} đã có {count} acc SUCCESS trước đó (vĩnh viễn).")
                        if count >= config.MAX_ACCOUNTS_PER_PROXY:
                            self.retired_indices.add(idx)
                            log.warning(f"   [Proxy Pool] Retire proxy index {idx} ngay khi khởi động (đã đạt {count}/{config.MAX_ACCOUNTS_PER_PROXY}).")
            
            total_permanent = sum(proxy_success_map.values())
            if total_permanent > 0:
                log.info(f"   [Proxy Pool] Đã khôi phục {total_permanent} lượt sử dụng vĩnh viễn từ dữ liệu kết quả.")
                
        except Exception as e:
            log.error(f"Lỗi đọc lịch sử proxy từ Sheets: {e}")

    def parse_proxy_string(self, proxy_str: str) -> dict | None:
        """
        Parse proxy string sang dict chuẩn Playwright.
        """
        try:
            if "@" in proxy_str:
                auth_part, host_part = proxy_str.split("@", 1)
                username, password = auth_part.split(":", 1)
                host, port = host_part.split(":", 1)
                return {
                    "server": f"http://{host}:{port}",
                    "username": username,
                    "password": password,
                    "raw": proxy_str
                }

            parts = proxy_str.split(":")
            if len(parts) == 2:
                return {
                    "server": f"http://{parts[0]}:{parts[1]}",
                    "raw": proxy_str
                }
            elif len(parts) == 4:
                return {
                    "server": f"http://{parts[0]}:{parts[1]}",
                    "username": parts[2],
                    "password": parts[3],
                    "raw": proxy_str
                }
            else:
                log.warning(f"Định dạng proxy không hợp lệ: {proxy_str}")
                return None
        except Exception as e:
            log.error(f"Lỗi parse proxy string '{proxy_str}': {e}")
            return None

    def get_next_proxy(self) -> tuple[dict | None, int | None]:
        """Lấy proxy tiếp theo theo cơ chế Round-robin (thread-safe).
        
        Mỗi lần gọi luôn lấy proxy khác nhau.
        Tự động bỏ qua các proxy đã retired, dead, HOẶC ĐANG ĐƯỢC WORKER KHÁC SỬ DỤNG.
        Proxy được trả về sẽ bị KHÓA (in_use) cho đến khi gọi release_proxy().
        """
        if not self.proxies:
            return None, None
            
        with self.lock:
            if self.circuit_open:
                log.error("🛑 Circuit breaker proxy đang mở: %s", self.circuit_reason)
                return None, None

            start_index = self.index
            while True:
                curr_idx = self.index
                # Luôn dịch index sang proxy tiếp theo cho các lần gọi sau (đảm bảo đổi proxy liên tục)
                self.index = (self.index + 1) % len(self.proxies)
                
                # BỎ QUA proxy đang bị worker khác dùng, đã retired, hoặc đã chết
                group_key = self.proxies[curr_idx].get("group_key", "")
                if (
                    curr_idx not in self.retired_indices
                    and curr_idx not in self.dead_indices
                    and curr_idx not in self.in_use_indices
                    and group_key not in self.blocked_groups
                ):
                    proxy = dict(self.proxies[curr_idx])
                    # Chỉ reserve để health-check; claim sau khi check thành công.
                    self.in_use_indices.add(curr_idx)
                    log.info(f"   -> Reserve proxy index={curr_idx} | {proxy.get('server')} để health-check")
                    return proxy, curr_idx
                
                # Nếu đã duyệt hết 1 vòng mà không tìm được con nào khả dụng
                if self.index == start_index:
                    eligible_in_use = any(
                        idx in self.in_use_indices
                        and idx not in self.dead_indices
                        and idx not in self.retired_indices
                        and self.proxies[idx].get("group_key", "") not in self.blocked_groups
                        for idx in range(len(self.proxies))
                    )
                    if eligible_in_use:
                        return "WAIT", -1
                        
                    log.error("❌ TẤT CẢ proxy sống đã được cấp cho tài khoản khác; không reset để tránh tái sử dụng proxy.")
                    return None, None

    def claim_proxy(self, proxy_index: int) -> bool:
        """Claim proxy sau health-check OK; từ đây proxy không được cấp lại."""
        if proxy_index is None or proxy_index < 0:
            return False
        with self.lock:
            group_key = self.proxies[proxy_index].get("group_key", "")
            if (
                self.circuit_open
                or proxy_index in self.dead_indices
                or group_key in self.blocked_groups
            ):
                self.in_use_indices.discard(proxy_index)
                return False
            self.claimed_indices.add(proxy_index)
            self.usage_counts[proxy_index] = max(1, self.usage_counts.get(proxy_index, 0))
            self.retired_indices.add(proxy_index)
            return True

    def mark_healthy(self, proxy_index: int):
        if proxy_index is None or proxy_index < 0:
            return
        with self.lock:
            group_key = self.proxies[proxy_index].get("group_key", "")
            if proxy_index in self.dead_indices or group_key in self.blocked_groups:
                return
            self.consecutive_failures = 0
            self.group_failures[group_key] = 0

    def _open_circuit_locked(self, reason: str):
        self.circuit_open = True
        self.circuit_reason = str(reason or "Kho proxy không ổn định")
        config.PROXY_CIRCUIT_OPEN = True
        config.PROXY_CIRCUIT_REASON = self.circuit_reason
        log.error("🛑 MỞ CIRCUIT BREAKER PROXY: %s", self.circuit_reason)

    def is_circuit_open(self) -> bool:
        with self.lock:
            return self.circuit_open

    def get_circuit_reason(self) -> str:
        with self.lock:
            return self.circuit_reason

    def is_group_blocked(self, proxy_index: int) -> bool:
        if proxy_index is None or proxy_index < 0:
            return False
        with self.lock:
            group_key = self.proxies[proxy_index].get("group_key", "")
            return group_key in self.blocked_groups

    def get_group_raws(self, proxy_index: int) -> list[dict]:
        if proxy_index is None or proxy_index < 0:
            return []
        with self.lock:
            group_key = self.proxies[proxy_index].get("group_key", "")
            return [
                {
                    "proxy_id": str(proxy.get("proxy_id") or ""),
                    "raw": str(proxy.get("raw") or ""),
                    "sheet_row": proxy.get("sheet_row"),
                }
                for proxy in self.proxies
                if proxy.get("group_key", "") == group_key and proxy.get("raw")
            ]

    def get_proxy_ref(self, proxy_index: int) -> dict:
        """Trả về định danh dòng Excel của một proxy slot."""
        if proxy_index is None or proxy_index < 0:
            return {}
        with self.lock:
            proxy = self.proxies[proxy_index]
            return {
                "proxy_id": str(proxy.get("proxy_id") or ""),
                "raw": str(proxy.get("raw") or ""),
                "sheet_row": proxy.get("sheet_row"),
            }

    def release_proxy(self, proxy_index: int):
        """MỞ KHÓA proxy sau khi worker xử lý xong 1 account (dù thành công hay thất bại).
        
        PHẢI gọi hàm này sau mỗi lần xử lý xong 1 account để worker khác có thể dùng proxy này.
        """
        if proxy_index is None or proxy_index < 0:
            return
        with self.lock:
            self.in_use_indices.discard(proxy_index)

    def mark_used(self, proxy_index: int):
        """Ghi nhận đã sử dụng xong 1 account (dù thành công hay thất bại).
        
        Tăng usage_count và retire nếu đạt giới hạn.
        LƯU Ý: Hàm này KHÔNG tự release proxy. Gọi release_proxy() riêng.
        """
        if proxy_index is None or proxy_index < 0:
            return
        with self.lock:
            self.consecutive_failures = 0  # Reset counter khi proxy chạy hết 1 luồng (sống)
            if proxy_index in self.claimed_indices:
                self.retired_indices.add(proxy_index)
                return
            count = 1
            self.usage_counts[proxy_index] = count
            self.claimed_indices.add(proxy_index)
            log.info(f"   [Proxy Pool] Proxy index {proxy_index} đã dùng {count}/{config.MAX_ACCOUNTS_PER_PROXY} lần.")
            if count >= config.MAX_ACCOUNTS_PER_PROXY:
                log.warning(f"   [Proxy Pool] Retire proxy index {proxy_index} (Đạt giới hạn tối đa {config.MAX_ACCOUNTS_PER_PROXY} lần sử dụng).")
                self.retired_indices.add(proxy_index)

    def set_permanent_count(self, proxy_ref, count: int):
        """Khôi phục proxy đã từng được gán từ sheet Accounts."""
        if count <= 0:
            return
        if isinstance(proxy_ref, dict):
            proxy_id = str(proxy_ref.get("proxy_id") or "").strip()
            proxy_raw = str(proxy_ref.get("raw") or "").strip()
        else:
            proxy_id = str(proxy_ref or "").strip()
            proxy_raw = str(proxy_ref or "").strip()
        with self.lock:
            for index, proxy in enumerate(self.proxies):
                id_matches = proxy_id and proxy.get("proxy_id") == proxy_id
                legacy_raw_matches = not proxy_id.startswith("PX-") and proxy.get("raw") == proxy_raw
                if id_matches or legacy_raw_matches:
                    self.usage_counts[index] = max(count, self.usage_counts.get(index, 0))
                    self.claimed_indices.add(index)
                    self.retired_indices.add(index)
                    log.info(f"   [Proxy Pool] Không dùng lại proxy index {index}: đã gán cho account trước đó.")
                    break

    def count(self) -> int:
        return len(self.proxies)

    def available_count(self) -> int:
        """Số proxy chưa từng được gán và có thể cấp cho account mới."""
        with self.lock:
            return sum(
                1
                for index in range(len(self.proxies))
                if index not in self.retired_indices
                and index not in self.dead_indices
                and index not in self.in_use_indices
                and self.proxies[index].get("group_key", "") not in self.blocked_groups
            )

    def mark_failed(self, proxy_index: int, reason: str = "", fatal_group: bool = False):
        """Đánh dấu proxy đã chết/lỗi để không sử dụng lại nữa."""
        if proxy_index is None or proxy_index < 0:
            return
        with self.lock:
            self.dead_indices.add(proxy_index)
            self.in_use_indices.discard(proxy_index)  # Mở khóa luôn vì proxy chết rồi
            self.consecutive_failures += 1
            group_key = self.proxies[proxy_index].get("group_key", "")
            group_label = self.proxies[proxy_index].get("group_label", "proxy pool")
            group_count = self.group_failures.get(group_key, 0) + 1
            self.group_failures[group_key] = group_count

            threshold = int(getattr(config, "PROXY_FAILURE_THRESHOLD", 3))
            # Lỗi của một dòng chỉ loại đúng dòng đó. Chỉ lỗi quota/xác thực
            # của gateway mới khóa toàn bộ các dòng chung tài khoản pool.
            if fatal_group:
                self.blocked_groups.add(group_key)
                for idx, proxy in enumerate(self.proxies):
                    if proxy.get("group_key", "") == group_key:
                        self.dead_indices.add(idx)
                        self.in_use_indices.discard(idx)
                log.error("🚫 Đã khóa toàn bộ nhóm proxy %s: %s", group_label, reason or "lỗi liên tiếp")

            if self.consecutive_failures >= threshold:
                self._open_circuit_locked(
                    f"{self.consecutive_failures} lỗi proxy liên tiếp; lỗi gần nhất: {reason or 'không rõ'}"
                )
            elif fatal_group and self.available_count_unlocked() == 0:
                self._open_circuit_locked(f"Nhóm proxy hết quota/xác thực: {group_label}")

            log.warning(
                "   [Proxy Pool] Loại proxy index %s (lỗi liên tiếp toàn hệ thống: %s/%s; nhóm: %s/%s)",
                proxy_index, self.consecutive_failures, threshold, group_count, threshold,
            )

    def available_count_unlocked(self) -> int:
        return sum(
            1
            for index in range(len(self.proxies))
            if index not in self.retired_indices
            and index not in self.dead_indices
            and index not in self.in_use_indices
            and self.proxies[index].get("group_key", "") not in self.blocked_groups
        )
