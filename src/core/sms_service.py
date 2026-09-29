from __future__ import annotations

import threading
import time
import requests
import json
import os
import random
from datetime import datetime
from pathlib import Path

import src.config as config
from src.utils.logger import get_logger

log = get_logger("sms_service")

def format_jp_phone(phone_raw: str) -> str:
    phone = str(phone_raw or "").strip().replace("+", "").replace("-", "").replace(" ", "")
    if phone.startswith("81"):
        phone = "0" + phone[2:]
    if not phone.startswith("0") and len(phone) in [9, 10]:
        phone = "0" + phone
    return phone



_BASE = config.SMS_BASE_URL.rstrip("/")
_apikey: str = ""
_apikey_expires: float = 0.0
_apikey_lock = threading.Lock()
_pre_fetched_lock = threading.Lock()
_manual_numbers_lock = threading.Lock()

_PREFETCH_STATUSES = {"available", "assigned", "used", "success", "failed"}


def _fallback_failure_detail(result: str) -> str:
    result = str(result or "FAILED").strip().upper()
    if result == "PENDING":
        return "Quy trình chưa hoàn tất; tài khoản được đưa về trạng thái PENDING."
    if result == "FAIL_NO_RETRY":
        return "Quy trình thất bại và được đánh dấu không thử lại."
    return f"Quy trình sử dụng SĐT không thành công (kết quả: {result})."


def _prefetched_path() -> Path:
    return config.DATA_DIR / "pre_fetched_numbers.json"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_prefetched_locked(records: list[dict]) -> None:
    """Ghi kho số atomically. Caller phải giữ ``_pre_fetched_lock``."""
    path = _prefetched_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp_path, path)


def _normalize_prefetched_record(item) -> dict | None:
    if isinstance(item, str):
        item = {"phone": item}
    if not isinstance(item, dict):
        return None

    phone = str(item.get("phone") or "").strip()
    if not phone:
        return None

    usage_status = str(item.get("usage_status") or "").strip().lower()
    if usage_status not in _PREFETCH_STATUSES:
        usage_status = "used" if item.get("is_used", False) else "available"

    provider_status = item.get("provider_status")
    if provider_status is None:
        # File cũ dùng `status` cho trạng thái do OTPBase trả về.
        provider_status = item.get("status")

    result = str(item.get("result") or "")
    error_details = str(item.get("error_details") or "")
    needs_migration = usage_status == "failed" and not error_details.strip()
    if needs_migration:
        error_details = _fallback_failure_detail(result)

    return {
        "phone": phone,
        "pkey": "OTP_WEB",
        "source": "otp_web",
        "provider_status": str(provider_status or ""),
        "usage_status": usage_status,
        "is_used": usage_status != "available",
        "assigned_email": str(item.get("assigned_email") or ""),
        "assigned_at": str(item.get("assigned_at") or ""),
        "finished_at": str(item.get("finished_at") or ""),
        "result": result,
        "error_details": error_details,
        "_needs_migration": needs_migration,
    }


def _load_prefetched_locked() -> tuple[list[dict], int, int]:
    """Đọc, migrate schema cũ và loại số trùng; caller giữ lock."""
    path = _prefetched_path()
    if not path.exists():
        return [], 0, 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Không thể đọc pre_fetched_numbers.json: {exc}") from exc
    if not isinstance(raw, list):
        raise RuntimeError("pre_fetched_numbers.json phải chứa một danh sách JSON")

    records_by_phone: dict[str, dict] = {}
    duplicate_count = 0
    migration_count = 0
    status_rank = {"available": 0, "assigned": 1, "used": 1, "failed": 2, "success": 3}
    for item in raw:
        record = _normalize_prefetched_record(item)
        if not record:
            continue
        if record.pop("_needs_migration", False):
            migration_count += 1
        key = format_jp_phone(record["phone"])
        existing = records_by_phone.get(key)
        if existing is None:
            records_by_phone[key] = record
            continue

        duplicate_count += 1
        # Một số đã từng được dùng không bao giờ được reset về available.
        if status_rank[record["usage_status"]] > status_rank[existing["usage_status"]]:
            records_by_phone[key] = record
            existing = record
        for field in (
            "provider_status", "assigned_email", "assigned_at",
            "finished_at", "result", "error_details",
        ):
            if not existing.get(field) and record.get(field):
                existing[field] = record[field]

    return list(records_by_phone.values()), duplicate_count, migration_count


def get_prefetched_phone_inventory() -> list[dict]:
    """Danh sách tracking đã chuẩn hóa để caller hiển thị."""
    with _pre_fetched_lock:
        records, duplicate_count, migration_count = _load_prefetched_locked()
        if duplicate_count or migration_count:
            _write_prefetched_locked(records)
            if duplicate_count:
                log.warning("📱 Đã loại %s dòng SĐT prefetch bị trùng.", duplicate_count)
            if migration_count:
                log.info("📱 Đã bổ sung chi tiết lỗi cho %s record SĐT cũ.", migration_count)
        return [dict(record) for record in records]


def get_prefetched_phone_stats() -> dict:
    records = get_prefetched_phone_inventory()
    stats = {
        "total": len(records),
        "available": 0,
        "assigned": 0,
        "success": 0,
        "failed": 0,
    }
    for record in records:
        status = record.get("usage_status")
        if status == "available":
            stats["available"] += 1
        elif status in ("assigned", "used"):
            stats["assigned"] += 1
        elif status == "success":
            stats["success"] += 1
        elif status == "failed":
            stats["failed"] += 1
    return stats


def merge_prefetched_phones(new_records: list[dict]) -> dict:
    """Bổ sung số mới từ OTPBase nhưng giữ nguyên toàn bộ lịch sử số cũ."""
    with _pre_fetched_lock:
        records, duplicate_count, _ = _load_prefetched_locked()
        by_phone = {format_jp_phone(record["phone"]): record for record in records}
        added = 0
        for item in new_records:
            incoming = _normalize_prefetched_record(item)
            if not incoming:
                continue
            incoming.pop("_needs_migration", None)
            key = format_jp_phone(incoming["phone"])
            existing = by_phone.get(key)
            if existing:
                existing["provider_status"] = incoming.get("provider_status", "")
                continue
            incoming["usage_status"] = "available"
            incoming["is_used"] = False
            by_phone[key] = incoming
            added += 1
        merged = list(by_phone.values())
        _write_prefetched_locked(merged)

    stats = get_prefetched_phone_stats()
    stats.update({"added": added, "duplicates_removed": duplicate_count})
    return stats


def sync_prefetched_phones(new_records: list[dict]) -> dict:
    """Đồng bộ kho số theo đúng danh sách API trả về.

    Khác ``merge_prefetched_phones`` (chỉ cộng dồn): hàm này coi API là nguồn
    sự thật, nên số không còn trên API sẽ bị XOÁ khỏi kho. Dùng cho nút
    "Cập nhật SĐT" — kéo lượt 2 về 30 số thì 20 số cũ không còn trên API sẽ
    biến mất thay vì nằm lại làm rác.

    Hai thứ được bảo vệ:

    - Số đang ở trạng thái ``assigned`` KHÔNG bị xoá dù vắng mặt trên API, vì
      có worker đang giữ và còn phải gọi ``finalize_prefetched_phone`` cho nó.
    - Số vẫn còn trên API thì giữ nguyên trạng thái sử dụng cũ; số đã dùng
      không bị đưa ngược về ``available``.
    """
    incoming: dict[str, dict] = {}
    for item in new_records:
        record = _normalize_prefetched_record(item)
        if not record:
            continue
        record.pop("_needs_migration", None)
        incoming[format_jp_phone(record["phone"])] = record

    with _pre_fetched_lock:
        records, duplicate_count, _ = _load_prefetched_locked()
        existing = {format_jp_phone(record["phone"]): record for record in records}

        kept: list[dict] = []
        added = 0
        removed = 0
        kept_assigned = 0

        for key, record in existing.items():
            if key in incoming:
                # Còn trên API — giữ nguyên lịch sử, chỉ cập nhật trạng thái nhà cung cấp.
                record["provider_status"] = incoming[key].get("provider_status", "")
                kept.append(record)
            elif record.get("usage_status") == "assigned":
                # Worker đang giữ số này, xoá bây giờ là mất dấu khi chốt kết quả.
                kept.append(record)
                kept_assigned += 1
                log.warning(
                    "⚠️ Số %s không còn trên API nhưng đang được dùng — giữ lại.",
                    record.get("phone"),
                )
            else:
                removed += 1

        for key, record in incoming.items():
            if key in existing:
                continue
            record["usage_status"] = "available"
            record["is_used"] = False
            kept.append(record)
            added += 1

        _write_prefetched_locked(kept)

    stats = get_prefetched_phone_stats()
    stats.update({
        "added": added,
        "removed": removed,
        "kept_assigned": kept_assigned,
        "duplicates_removed": duplicate_count,
    })
    log.info(
        "🔄 Đồng bộ kho số: thêm %s, xoá %s, giữ vì đang dùng %s → tổng %s (sẵn sàng %s)",
        added, removed, kept_assigned, stats["total"], stats["available"],
    )
    return stats


def reserve_prefetched_phone(email: str = "") -> dict:
    """Atomically gán random một số chưa dùng cho một account."""
    with _pre_fetched_lock:
        records, duplicate_count, _ = _load_prefetched_locked()
        candidates = [record for record in records if record.get("usage_status") == "available"]
        selected = random.choice(candidates) if candidates else None
        if selected:
            selected["usage_status"] = "assigned"
            selected["is_used"] = True
            selected["assigned_email"] = email or "active_worker"
            selected["assigned_at"] = _now_iso()
            selected["finished_at"] = ""
            selected["result"] = "PROCESSING"
            selected["error_details"] = ""
        _write_prefetched_locked(records)

    if not selected:
        raise RuntimeError(
            "Hết số lấy trước từ OTPBase. Hãy bấm 'Tải trước số' rồi chạy lại."
        )
    remaining = sum(1 for record in records if record.get("usage_status") == "available")
    log.info(
        "📱 Gán số lấy trước %s cho %s (còn %s số sẵn sàng)",
        selected["phone"], email or "worker", remaining,
    )
    return dict(selected)


def mark_phone_dead(phone: str, reason: str = "") -> bool:
    """Danh dau mot SIM la KHONG DUNG DUOC NUA (khong nhan duoc SMS).

    Kho SIM co nhung so van nam trong danh sach nhung thuc te khong nhan duoc
    tin (da gap 07093913839 chet 2/2 lan). Neu khong loai ra, moi lan chay lai
    deu co the boc trung no va dot them 300 giay cho + mot account.
    """
    if not phone:
        return False
    target = format_jp_phone(phone)
    with _pre_fetched_lock:
        records, _, _ = _load_prefetched_locked()
        for record in records:
            if format_jp_phone(record.get("phone", "")) != target:
                continue
            record.update(
                usage_status="failed", is_used=True, result="FAILED",
                finished_at=_now_iso(),
                error_details=reason or "Khong nhan duoc SMS",
            )
            _write_prefetched_locked(records)
            log.warning("🚫 Loại SIM %s khỏi kho: %s", target, reason or "không nhận được SMS")
            return True
    return False


def _to_utc_compare(local_iso: str) -> str:
    """Doi moc 'assigned_at' (gio may, co offset) sang chuoi UTC de so voi API."""
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(str(local_iso)).astimezone(timezone.utc).isoformat()
    except Exception:
        return "0000"


def release_prefetched_phone(phone: str) -> bool:
    """Tra mot so ve trang thai available khi CHUA he gui SMS.

    Kho SIM cua mot token la huu han (vd 48 so cho 47 account), trong khi
    ``order_phone()`` bat buoc phai cap so TRUOC khi submit form — vi so phai
    duoc dien vao o ``tel1``. Neu form bi site tu choi (loi dia chi, loi kana...)
    thi so do chua bi dung mot lan nao: khong tra lai la dot sach kho chi vi vai
    account loi du lieu.

    CHI goi khi chac chan chua co SMS nao duoc gui toi so nay.
    """
    if not phone:
        return False
    target = format_jp_phone(phone)

    # CHI tra so ve kho khi NO CHUA HE NHAN TIN NAO ke tu luc duoc cap. Mot so
    # Neu da co SMS gui toi thi so co the dang gan voi mot phien dang ky treo;
    # cap lai cho account khac co the khong nhan duoc tin moi.
    with _pre_fetched_lock:
        records, _, _ = _load_prefetched_locked()
        assigned_at = next(
            (r.get("assigned_at") for r in records
             if format_jp_phone(r.get("phone", "")) == target), ""
        )
    if assigned_at:
        try:
            import src.core.otp_web_client as otp_web_client
            data = otp_web_client._request_messages(target, limit=5)
            for message in (data.get("messages") or []):
                if str(message.get("received_at") or "") >= _to_utc_compare(assigned_at):
                    log.warning(
                        "⛔ Không trả số %s về kho: đã có SMS gửi tới sau khi cấp "
                        "(số đã gắn với một đơn đăng ký treo).", target,
                    )
                    return False
        except Exception as exc:
            log.debug("Không kiểm tra được lịch sử tin của %s: %s", target, exc)

    with _pre_fetched_lock:
        records, _, _ = _load_prefetched_locked()
        for record in records:
            if format_jp_phone(record.get("phone", "")) != target:
                continue
            if record.get("usage_status") not in ("assigned", "used"):
                return False
            record.update(
                usage_status="available", is_used=False, assigned_email="",
                assigned_at="", finished_at="", result="", error_details="",
            )
            _write_prefetched_locked(records)
            log.info("♻️ Tra so %s ve kho (chua gui SMS lan nao).", target)
            return True
    return False


def recycle_prefetched_phone(phone: str, reason: str = "") -> bool:
    """Trả số chưa xác thực thành công về kho, kể cả đã từng kích hoạt SMS.

    Việc số được cấp cho worker hoặc provider đã gửi mã không có nghĩa số đã
    gắn thành công với account. Chỉ SUCCESS hoặc lỗi website xác nhận số thuộc
    account khác mới không được recycle.
    """
    if not phone:
        return False
    target = format_jp_phone(phone)
    with _pre_fetched_lock:
        records, _, _ = _load_prefetched_locked()
        for record in records:
            if format_jp_phone(record.get("phone", "")) != target:
                continue
            if record.get("usage_status") == "success":
                log.warning("⛔ Không recycle số %s vì đã SUCCESS.", target)
                return False
            record.update(
                usage_status="available",
                is_used=False,
                assigned_email="",
                assigned_at="",
                finished_at="",
                result="",
                error_details="",
            )
            _write_prefetched_locked(records)
            log.info(
                "♻️ Trả số %s về kho để dùng lại%s.",
                target,
                f" ({str(reason)[:120]})" if reason else "",
            )
            return True
    return False


def finalize_prefetched_phone(
    phone: str,
    email: str = "",
    result: str = "FAILED",
    error_details: str = "",
) -> bool:
    """Chốt kết quả số đã gán; tuyệt đối không đưa số trở lại available."""
    if not phone:
        return False
    result = str(result or "FAILED").upper()
    with _pre_fetched_lock:
        records, _, migration_count = _load_prefetched_locked()
        key = format_jp_phone(phone)
        updated = False
        for record in records:
            if format_jp_phone(record.get("phone")) != key:
                continue
            if record.get("usage_status") == "success" and result != "SUCCESS":
                return True
            record["usage_status"] = "success" if result == "SUCCESS" else "failed"
            record["is_used"] = True
            if email:
                record["assigned_email"] = email
            if not record.get("assigned_at"):
                record["assigned_at"] = _now_iso()
            record["finished_at"] = _now_iso()
            record["result"] = result
            detail = str(error_details or "").strip()
            if result != "SUCCESS" and not detail:
                detail = _fallback_failure_detail(result)
            record["error_details"] = detail[:500]
            updated = True
            break
        if updated or migration_count:
            _write_prefetched_locked(records)
        return updated


def set_prefetched_phone_status(
    phones,
    usage_status: str,
    error_details: str = "",
) -> dict:
    """Đặt tay trạng thái cho một hoặc nhiều số trong kho (màn Theo dõi SĐT).

    Kho có thể còn record kẹt ở ``assigned`` khi một phiên bị giết giữa chừng
    hoặc khi worker không chốt được số. Không có đường nào tự dọn: ``sync``
    CỐ TÌNH bảo vệ ``assigned`` để không giật số khỏi worker đang chạy, nên
    những record đó nằm lì và chiếm chỗ vĩnh viễn. Đây là cửa duy nhất để sửa.

    Trả về ``{"updated": n, "missing": [...]}``. Các trường phái sinh
    (``is_used``, ``result``, ``finished_at``...) được đặt cho khớp trạng thái
    mới, đúng như khi luồng chạy thật chốt số — nếu chỉ đổi mỗi
    ``usage_status`` thì thống kê và bộ lọc sẽ mâu thuẫn với dữ liệu.
    """
    target = str(usage_status or "").strip().lower()
    if target not in _PREFETCH_STATUSES:
        raise ValueError(
            f"Trạng thái không hợp lệ: {usage_status!r}. "
            f"Chỉ nhận: {', '.join(sorted(_PREFETCH_STATUSES))}"
        )

    if isinstance(phones, str):
        phones = [phones]
    wanted = {format_jp_phone(p) for p in phones if str(p or "").strip()}
    if not wanted:
        return {"updated": 0, "missing": []}

    detail = str(error_details or "").strip()
    seen: set[str] = set()

    with _pre_fetched_lock:
        records, _, _ = _load_prefetched_locked()
        updated = 0
        for record in records:
            key = format_jp_phone(record.get("phone", ""))
            if key not in wanted:
                continue
            seen.add(key)

            record["usage_status"] = target
            record["is_used"] = target != "available"

            if target == "available":
                # Trả về kho = xoá sạch dấu vết lần dùng trước, giống
                # release_prefetched_phone; giữ lại là lần gán sau đọc nhầm.
                record.update(
                    assigned_email="", assigned_at="", finished_at="",
                    result="", error_details="",
                )
            elif target == "assigned":
                record["result"] = "PROCESSING"
                record["finished_at"] = ""
                record["error_details"] = detail
                if not record.get("assigned_at"):
                    record["assigned_at"] = _now_iso()
            else:
                record["result"] = "SUCCESS" if target == "success" else "FAILED"
                record["finished_at"] = _now_iso()
                if not record.get("assigned_at"):
                    record["assigned_at"] = _now_iso()
                if target == "success":
                    record["error_details"] = detail
                else:
                    record["error_details"] = (
                        detail or _fallback_failure_detail(record["result"])
                    )[:500]
            updated += 1

        if updated:
            _write_prefetched_locked(records)
            log.info(
                "✏️ Đã đổi trạng thái %s số sang %r%s.",
                updated, target, f" ({detail[:80]})" if detail else "",
            )

    return {"updated": updated, "missing": sorted(wanted - seen)}


def validate_prefetched_capacity(required_accounts: int) -> dict:
    """Chặn phiên chạy trước khi tạo worker nếu kho số không đủ."""
    if not getattr(config, "OTP_WEB_ENABLED", False):
        raise RuntimeError("OTP Web đang tắt. Hãy bật OTP Web trước khi chạy.")
    if not str(getattr(config, "OTP_WEB_API_KEY", "") or "").strip():
        raise RuntimeError("Chưa có OTP API key. Hãy nhập API key và lưu cấu hình.")
    stats = get_prefetched_phone_stats()
    required = max(0, int(required_accounts))
    if stats["available"] < required:
        missing = required - stats["available"]
        raise RuntimeError(
            "Không đủ số điện thoại OTPBase.\n\n"
            f"Cần: {required} số\n"
            f"Hiện có: {stats['available']} số sẵn sàng\n"
            f"Thiếu: {missing} số\n\n"
            "Hãy bấm 'Tải trước số' trước khi chạy."
        )
    return stats

def reset_manual_phone_in_use_flags():
    """Xóa tất cả cờ in_use_by tạm thời khi khởi động bot."""
    manual_path = config.DATA_DIR / "manual_phone_numbers.json"
    with _manual_numbers_lock:
        if not manual_path.exists(): return
        try:
            with open(manual_path, "r", encoding="utf-8") as f:
                numbers = json.load(f)
            if isinstance(numbers, list):
                updated = False
                for item in numbers:
                    if isinstance(item, dict) and item.get("in_use_by"):
                        item["in_use_by"] = None
                        updated = True
                if updated:
                    with open(manual_path, "w", encoding="utf-8") as f:
                        json.dump(numbers, f, indent=4, ensure_ascii=False)
        except Exception:
            pass

def get_unused_manual_phone_count() -> int:
    """
    Trả về số lượng SĐT thủ công chưa hoàn thành OTP (is_used = False và chưa bị giữ).
    """
    manual_path = config.DATA_DIR / "manual_phone_numbers.json"
    with _manual_numbers_lock:
        if not manual_path.exists():
            return 0
        try:
            with open(manual_path, "r", encoding="utf-8") as f:
                numbers = json.load(f)
            if isinstance(numbers, list):
                count = 0
                for item in numbers:
                    if isinstance(item, str):
                        count += 1
                    elif isinstance(item, dict):
                        if not item.get("is_used", False) and not item.get("in_use_by"):
                            count += 1
                return count
        except Exception:
            pass
        return 0

def get_manual_phone(email: str = "") -> dict:
    """
    Tạm giữ số điện thoại thủ công cho worker (đặt in_use_by = email).
    Chỉ khi nhập OTP thành công mới gọi confirm_manual_phone để đánh dấu is_used = True.
    """
    manual_path = config.DATA_DIR / "manual_phone_numbers.json"
    with _manual_numbers_lock:
        if not manual_path.exists():
            config.STOP_FLAG = True
            raise RuntimeError("Chưa có danh sách số điện thoại thủ công. Hãy tạo data/manual_phone_numbers.json trước khi chạy.")

        try:
            with open(manual_path, "r", encoding="utf-8") as f:
                raw_numbers = json.load(f)
        except Exception as e:
            config.STOP_FLAG = True
            raise RuntimeError(f"Không thể đọc file manual_phone_numbers.json: {e}")

        if not isinstance(raw_numbers, list) or len(raw_numbers) == 0:
            config.STOP_FLAG = True
            raise RuntimeError("Danh sách số điện thoại thủ công đang trống!")

        # Chuẩn hóa về list[dict]
        numbers = []
        for item in raw_numbers:
            if isinstance(item, str):
                numbers.append({"phone": item, "is_used": False, "in_use_by": None})
            elif isinstance(item, dict):
                numbers.append(item)

        valid_num = None
        for item in numbers:
            if not item.get("is_used", False) and not item.get("in_use_by"):
                item["in_use_by"] = email or "active_worker"
                valid_num = item
                break

        unused_count = sum(1 for n in numbers if not n.get("is_used", False) and not n.get("in_use_by"))

        with open(manual_path, "w", encoding="utf-8") as f:
            json.dump(numbers, f, indent=4, ensure_ascii=False)

        if not valid_num:
            config.STOP_FLAG = True
            log.warning("🛑 Hết số điện thoại thủ công khả dụng! Kích hoạt STOP_FLAG để dừng toàn bộ tiến trình.")
            raise RuntimeError("❌ Hết số điện thoại thủ công khả dụng trong danh sách! Dừng bot.")

        phone_str = valid_num.get("phone", "").strip()
        log.info(f"📱 [SĐT Thủ Công] Giữ số: {phone_str} cho {email or 'Worker'} (còn rảnh {unused_count} số chưa dùng)")
        return {
            "phone": phone_str,
            "pkey": "MANUAL",
            "price": 0,
            "balance": 0,
            "expires_at": 0,
        }

def confirm_manual_phone(phone: str):
    """
    Xác nhận OTP thành công -> Đánh dấu SĐT là is_used = True (hoàn tất hẳn).
    """
    if not phone: return
    manual_path = config.DATA_DIR / "manual_phone_numbers.json"
    with _manual_numbers_lock:
        if not manual_path.exists(): return
        try:
            with open(manual_path, "r", encoding="utf-8") as f:
                numbers = json.load(f)
            if not isinstance(numbers, list): return

            clean_p = format_jp_phone(phone).replace("-", "").replace(" ", "")
            updated = False
            for item in numbers:
                if isinstance(item, dict):
                    item_p = format_jp_phone(item.get("phone", "")).replace("-", "").replace(" ", "")
                    if item_p == clean_p or item.get("phone") == phone:
                        item["is_used"] = True
                        item["in_use_by"] = None
                        updated = True
                        break
            if updated:
                with open(manual_path, "w", encoding="utf-8") as f:
                    json.dump(numbers, f, indent=4, ensure_ascii=False)
                log.info(f"📱 [SĐT Thủ Công] ✅ OTP Thành công! Đã đánh dấu SĐT {phone} là ĐÃ HOÀN THÀNH (is_used = True)")
        except Exception as e:
            log.warning(f"⚠️ Lỗi khi confirm_manual_phone ({phone}): {e}")

def release_manual_phone(phone: str):
    """
    Nhả SĐT về trạng thái rảnh (in_use_by = None) nếu chưa hoàn thành OTP.
    """
    if not phone: return
    manual_path = config.DATA_DIR / "manual_phone_numbers.json"
    with _manual_numbers_lock:
        if not manual_path.exists(): return
        try:
            with open(manual_path, "r", encoding="utf-8") as f:
                numbers = json.load(f)
            if not isinstance(numbers, list): return

            clean_p = format_jp_phone(phone).replace("-", "").replace(" ", "")
            updated = False
            for item in numbers:
                if isinstance(item, dict):
                    item_p = format_jp_phone(item.get("phone", "")).replace("-", "").replace(" ", "")
                    if item_p == clean_p or item.get("phone") == phone:
                        if not item.get("is_used", False):
                            item["in_use_by"] = None
                            updated = True
                        break
            if updated:
                with open(manual_path, "w", encoding="utf-8") as f:
                    json.dump(numbers, f, indent=4, ensure_ascii=False)
                log.info(f"📱 [SĐT Thủ Công] 🔄 Đã nhả số {phone} về danh sách rảnh do chưa hoàn tất OTP")
        except Exception as e:
            log.warning(f"⚠️ Lỗi khi release_manual_phone ({phone}): {e}")




_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

def _get_apikey(force_refresh: bool = False) -> str:
    """Get apikey from cache or API. Thread-safe."""
    global _apikey, _apikey_expires
    
    with _apikey_lock:
        now = time.time()
        
        if not force_refresh:
            # 1. Memory cache check
            if _apikey and now < _apikey_expires - 300:
                return _apikey

            # 2. Disk cache check (từ config.json)
            try:
                # Reload config.json to get the latest changes (nếu user sửa tay)
                if config.CONFIG_FILE.exists():
                    with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
                        config._cfg = json.load(f)

                file_key = config._cfg.get("sms_api_key", "")
                file_expires_raw = config._cfg.get("sms_api_key_expires", 0.0)
                
                if isinstance(file_expires_raw, str) and file_expires_raw:
                    try:
                        import datetime
                        dt = datetime.datetime.strptime(file_expires_raw, "%H:%M:%S %d/%m/%Y")
                        file_expires = dt.timestamp()
                    except:
                        file_expires = time.time() + 31536000
                elif not file_expires_raw and file_key:
                    # Nếu user điền key nhưng không điền hạn, mặc định cho sống 1 năm
                    file_expires = time.time() + 31536000
                else:
                    file_expires = file_expires_raw / 1000.0 if file_expires_raw > 1e10 else file_expires_raw
                
                if file_key and now < file_expires - 300:
                    _apikey = file_key
                    _apikey_expires = file_expires
                    return _apikey
            except Exception as e:
                log.warning(f"Không đọc được cache apikey từ config.json: {e}")

        # 3. Request new apikey
        if not config.SMS_USERNAME or not config.SMS_PASSWORD:
            raise RuntimeError("SMS_USERNAME / SMS_PASSWORD chưa cấu hình trong config.json")

        log.info("Lấy SMS apikey mới từ API...")
        resp = requests.post(
            f"{_BASE}/api/ext/getKey",
            json={"username": config.SMS_USERNAME, "password": config.SMS_PASSWORD},
            headers=_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "success":
            raise RuntimeError(f"getKey thất bại: {data}")

        _apikey = data["apikey"]
        _apikey_expires = data.get("expires_at", 0.0) / 1000.0
        
        try:
            # Lưu file_expires dưới dạng string như user muốn
            expires_str = time.strftime('%H:%M:%S %d/%m/%Y', time.localtime(_apikey_expires))
            # Loại bỏ số 0 ở tháng/ngày nếu có để giống 12/7/2026
            expires_str = expires_str.replace('/0', '/')
            
            # Ghi trực tiếp vào config._cfg và lưu ra config.json
            config._cfg["sms_api_key"] = _apikey
            config._cfg["sms_api_key_expires"] = expires_str
            with open(config.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config._cfg, f, indent=4, ensure_ascii=False)
            log.info("Đã lưu SMS apikey mới vào config.json.")
        except Exception as e:
            log.warning(f"Không ghi được file config.json: {e}")

        expires_log = time.strftime('%H:%M:%S %d/%m/%Y', time.localtime(_apikey_expires)).replace('/0', '/')
        log.info(f"✅ Apikey OK (expires at: {expires_log})")
        return _apikey

def invalidate_apikey():
    """Clear memory and disk cache of apikey."""
    global _apikey, _apikey_expires
    with _apikey_lock:
        _apikey = ""
        _apikey_expires = 0.0
        try:
            if "sms_api_key" in config._cfg:
                config._cfg["sms_api_key"] = ""
            if "sms_api_key_expires" in config._cfg:
                config._cfg["sms_api_key_expires"] = ""
            
            with open(config.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config._cfg, f, indent=4, ensure_ascii=False)
            log.info("🧹 Đã xoá cache API Key do nghi ngờ bị lỗi hoặc hết hạn.")
        except Exception as e:
            log.warning(f"Không xóa được cache apikey trong config.json: {e}")

def check_balance(force_refresh=False) -> int:
    """Check SMS API balance."""
    try:
        apikey = _get_apikey(force_refresh=force_refresh)
        resp = requests.get(
            f"{_BASE}/api/ext/balance",
            params={"apikey": apikey},
            headers=_HEADERS,
            timeout=10,
        )
        try:
            data = resp.json()
        except Exception as json_e:
            log.error(f"❌ Phản hồi không phải JSON: {resp.status_code} - {resp.text[:200]}")
            raise json_e
        if data.get("status") == "success":
            balance = data["balance"]
            log.info(f"💰 Số dư SMS: {balance:,} điểm/yên")
            return balance
        else:
            msg = data.get("msg", "") or data.get("message", "")
            is_key_error = any(x in msg.lower() for x in ["api key", "hết hạn", "hợp lệ", "invalid", "expire"])
            if is_key_error:
                invalidate_apikey()
                if not force_refresh:
                    log.info("API Key hết hạn hoặc không hợp lệ, thử lấy lại key mới...")
                    return check_balance(force_refresh=True)
            log.error(f"❌ Lỗi API SMS: {data}")
    except Exception as e:
        log.error(f"❌ Lỗi khi lấy số dư: {e}")
    return -1

def order_phone(
    country: str | None = None,
    service_id: str | None = None,
    server: str | None = None,
    force_api: bool = False,
    email: str = "",
) -> dict:
    """
    Order phone number. Poll until phone number is ready.
    """
    if getattr(config, "USE_MANUAL_PHONE_LIST", False) and not force_api:
        return get_manual_phone(email=email)


    if getattr(config, "USE_PRE_FETCHED_NUMBERS", False) and not force_api:
        return reserve_prefetched_phone(email=email)

    def _do_order(force=False):
        apikey = _get_apikey(force_refresh=force)
        params = {
            "apikey":    apikey,
            "serviceId": service_id or config.SMS_SERVICE_ID,
            "server":    server     or config.SMS_SERVER,
            "country":   country    or config.SMS_COUNTRY,
        }
        resp = requests.get(f"{_BASE}/api/ext/order", params=params, headers=_HEADERS, timeout=15)
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            try:
                err_data = resp.json()
                log.error(f"HTTP {resp.status_code} Error: {err_data}")
                return err_data, params
            except Exception:
                pass
            raise e
        return resp.json(), params

    log.info(f"📱 Order phone | country={country or config.SMS_COUNTRY} serviceId={service_id or config.SMS_SERVICE_ID}")
    data, params = _do_order()

    if data.get("status") != "success":
        msg = data.get("message", "") or data.get("msg", "")
        is_key_error = any(x in msg.lower() for x in ["api key", "hết hạn", "hợp lệ", "invalid", "expire"])
        if is_key_error:
            invalidate_apikey()
            log.info(f"order thất bại do key ({msg}). Thử lấy lại API key mới...")
            data, params = _do_order(force=True)
            if data.get("status") != "success":
                raise RuntimeError(f"order thất bại: {data.get('message', data)}")
        else:
            raise RuntimeError(f"order thất bại: {data.get('message', data)}")
    
    apikey = params["apikey"]

    pkey = data["pkey"]
    phone = data.get("phone", "").strip()

    # If phone is not ready, poll getSms to retrieve the actual number
    if not phone or "xin số" in phone or not any(c.isdigit() for c in phone):
        log.info("📱 Số điện thoại chưa sẵn sàng, đang poll getSms để lấy số thực tế...")
        max_attempts = 12
        for attempt in range(1, max_attempts + 1):
            time.sleep(10)
            try:
                get_resp = requests.get(
                    f"{_BASE}/api/ext/getSms",
                    params={"apikey": apikey, "pkey": pkey},
                    headers=_HEADERS,
                    timeout=10,
                )
                get_data = get_resp.json()
                curr_phone = get_data.get("phone", "").strip()
                if curr_phone and "xin số" not in curr_phone and any(c.isdigit() for c in curr_phone):
                    phone = curr_phone
                    log.info(f"  [SUCCESS] Lấy số thực tế thành công: {phone} (attempt {attempt})")
                    break
            except Exception as e:
                log.warning(f"  Poll số điện thoại thất bại (attempt {attempt}): {e}")
        
        if not phone or "xin số" in phone or not any(c.isdigit() for c in phone):
            log.error("❌ Không lấy được số điện thoại thực tế từ API sau 120s — đang hủy số hoàn tiền...")
            try:
                requests.get(f"{_BASE}/api/ext/cancel", params={"apikey": apikey, "pkey": pkey}, headers=_HEADERS, timeout=10)
            except Exception as ce:
                log.warning(f"  Không thể hủy số: {ce}")
            raise RuntimeError("Không lấy được số điện thoại thực tế từ API!")

    log.info(f"✅ Phone: {phone} | pkey: {pkey[:12]}... | price: {data.get('price', 0)}")
    return {
        "phone":      phone,
        "pkey":       pkey,
        "price":      data.get("price", 0),
        "balance":    data.get("balance", 0),
        "expires_at": data.get("expires_at", 0),
    }

def poll_sms_otp(
    pkey: str,
    timeout: int = 300,
    poll_interval: int = 4,
) -> str | None:
    """
    Poll getSms until OTP is received or timeout is reached.
    """
    apikey = _get_apikey()
    deadline = time.time() + timeout
    log.info(f"⏳ Poll OTP | pkey: {pkey[:12]}... | timeout: {timeout}s")

    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{_BASE}/api/ext/getSms",
                params={"apikey": apikey, "pkey": pkey},
                headers=_HEADERS,
                timeout=10,
            )
            data = resp.json()

            if data.get("status") == "error":
                msg = data.get("msg", "") or data.get("message", "")
                is_key_error = any(x in msg.lower() for x in ["api key", "hết hạn", "hợp lệ", "invalid", "expire"])
                if is_key_error:
                    invalidate_apikey()
                    log.info("API Key hết hạn hoặc không hợp lệ khi poll OTP, đang lấy lại key mới...")
                    apikey = _get_apikey(force_refresh=True)
                    continue

            otp = data.get("otp", "")
            state = data.get("state", "")
            log.debug(f"  getSms: state='{state}' otp='{otp}'")

            if otp and state == "Hoàn thành":
                log.info(f"✅ SMS OTP: {otp}")
                return otp

        except Exception as e:
            log.warning(f"  getSms lỗi: {e}")

        remaining = int(deadline - time.time())
        if remaining <= 0:
            break
        # Chờ ngắt quãng để phản hồi nút STOP ngay lập tức
        import src.config as config
        stop_requested = False
        for _ in range(int(poll_interval * 2)):
            if config.STOP_FLAG:
                stop_requested = True
                break
            time.sleep(0.5)
        if stop_requested:
            log.warning("🛑 Nhận lệnh STOP, dừng chờ OTP SMS.")
            break

    log.warning(f"⏰ Timeout {timeout}s — không nhận được SMS OTP")
    return None

def cancel(pkey: str, phone: str = "") -> bool:
    """Cancel order and refund if no OTP received."""
    if pkey == "OTP_WEB":
        if phone:
            finalize_prefetched_phone(
                phone,
                result="CANCELLED",
                error_details="Số bị hủy hoặc không nhận được OTP",
            )
        return True
    if pkey in ("MANUAL", ""):
        if phone:
            release_manual_phone(phone)
        return True

    try:
        apikey = _get_apikey()

        resp = requests.get(
            f"{_BASE}/api/ext/cancel",
            params={"apikey": apikey, "pkey": pkey},
            headers=_HEADERS,
            timeout=10,
        )
        data = resp.json()
        if data.get("status") == "success":
            log.info(f"✅ Hủy số thành công | balance: {data.get('balance', '?')}")
            return True
        log.warning(f"Hủy số thất bại: {data.get('message', data)}")
    except Exception as e:
        log.warning(f"cancel lỗi: {e}")
    return False
