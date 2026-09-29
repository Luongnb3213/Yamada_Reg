"""Client đọc OTP từ OTP Web cho luồng dùng SIM thủ công.

Client chụp ``seq`` hiện tại trước khi website gửi SMS, sau đó chỉ poll các tin
mới hơn mốc đó. Nhờ vậy một worker không thể lấy nhầm OTP cũ của cùng SIM.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import requests

import src.config as config
from src.utils.logger import get_logger


log = get_logger("otp_web_client")


class OTPWebError(RuntimeError):
    """Lỗi kết nối, xác thực hoặc dữ liệu trả về từ OTP Web."""


class OTPWebTimeout(OTPWebError):
    """Không nhận được OTP mới trong thời gian cho phép."""


def is_configured() -> bool:
    """Trả về True khi chế độ tự lấy OTP Web đã có đủ URL và API key."""
    return bool(
        getattr(config, "OTP_WEB_ENABLED", True)
        and getattr(config, "OTP_WEB_URL", "")
        and getattr(config, "OTP_WEB_API_KEY", "")
    )


def test_api_access() -> dict:
    """Kiểm tra URL/API key mà không yêu cầu hoặc hiển thị OTP của số cụ thể."""
    if not is_configured():
        raise OTPWebError("Chưa cấu hình OTP Web URL/API key")
    return _request_messages("", limit=1)


def fetch_phones(limit: int = 500, status: str | None = None) -> dict:
    """Lấy danh sách số có sẵn từ OTPBase bằng chính OTP Web API key."""
    if not is_configured():
        raise OTPWebError("Chưa cấu hình OTP Web URL/API key")

    params: dict[str, Any] = {"detail": 1, "limit": max(1, min(int(limit), 2000))}
    if status:
        params["status"] = status
    url = f"{config.OTP_WEB_URL.rstrip('/')}/api/v1/phones"
    try:
        response = requests.get(
            url,
            params=params,
            headers={"x-api-key": config.OTP_WEB_API_KEY},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise OTPWebError(f"Không kết nối được OTPBase: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise OTPWebError(
            f"OTPBase trả dữ liệu không hợp lệ (HTTP {response.status_code})"
        ) from exc
    if response.status_code == 401:
        raise OTPWebError("OTPBase API key không đúng hoặc đã bị thu hồi")
    if not response.ok or not data.get("ok"):
        raise OTPWebError(data.get("error") or f"OTPBase trả HTTP {response.status_code}")
    if not isinstance(data.get("phones"), list):
        raise OTPWebError("OTPBase không trả về danh sách phones hợp lệ")
    return data


def _request_messages(phone: str, **params: Any) -> dict:
    url = f"{config.OTP_WEB_URL}/api/v1/messages"
    query = {"phone": phone, **params}

    try:
        response = requests.get(
            url,
            params=query,
            headers={"x-api-key": config.OTP_WEB_API_KEY},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise OTPWebError(f"Không kết nối được OTP Web: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise OTPWebError(
            f"OTP Web trả dữ liệu không hợp lệ (HTTP {response.status_code})"
        ) from exc

    if response.status_code == 401:
        raise OTPWebError("OTP Web API key không đúng hoặc đã bị thu hồi")
    if response.status_code == 403:
        raise OTPWebError(
            f"Số {phone} chưa được cấp quyền cho API key đang dùng"
        )
    if not response.ok or not data.get("ok"):
        raise OTPWebError(
            data.get("error") or f"OTP Web trả HTTP {response.status_code}"
        )

    return data


async def capture_cursor(phone: str) -> int:
    """Lấy seq mới nhất của SIM ngay trước lúc website gửi OTP."""
    if not is_configured():
        raise OTPWebError("Chưa cấu hình OTP Web URL/API key")

    data = await asyncio.to_thread(_request_messages, phone, limit=1)
    seqs = [
        int(message.get("seq", 0))
        for message in data.get("messages", [])
        if str(message.get("seq", "")).isdigit()
    ]
    cursor = max(seqs, default=int(data.get("last_seq") or 0))
    log.info(f"📡 OTP Web đã đặt mốc seq={cursor} cho số {phone}")
    return cursor


def _looks_like_target(message: dict) -> bool:
    """Ưu tiên OTP khớp keyword cấu hình, nếu có."""
    hints = tuple(getattr(config, "OTP_SOURCE_KEYWORDS", ()) or ())
    if not hints:
        return False
    haystack = " ".join(
        str(message.get(key) or "")
        for key in ("service", "sender", "content")
    ).casefold()
    return any(str(hint).casefold() in haystack for hint in hints)


async def wait_for_otp(
    phone: str,
    after_seq: int,
    timeout: int | None = None,
    after_time: str | None = None,
) -> str:
    """Poll OTP Web tới khi có OTP mới của đúng số điện thoại.

    Nếu trong một lần poll có nhiều OTP, ưu tiên tin khớp ``otp_source_keywords``
    trong config. Khi OTP Web chưa gắn nhãn dịch vụ, lấy OTP mới nhất vì
    ``phone`` và ``after_seq`` đã giới hạn chính xác SIM và thời điểm worker.
    """
    if not is_configured():
        raise OTPWebError("Chưa cấu hình OTP Web URL/API key")

    deadline = time.monotonic() + int(timeout or config.OTP_WEB_TIMEOUT)
    cursor = max(0, int(after_seq or 0))
    poll_interval = float(config.OTP_WEB_POLL_INTERVAL)
    transient_errors = 0

    log.info(
        f"📡 Đang chờ OTP Web cho số {phone} "
        f"(sau seq={cursor}, timeout={int(timeout or config.OTP_WEB_TIMEOUT)}s)..."
    )

    while time.monotonic() < deadline:
        if getattr(config, "STOP_FLAG", False):
            raise OTPWebError("STOP_FLAG")

        try:
            data = await asyncio.to_thread(
                _request_messages,
                phone,
                otp=1,
                after_seq=cursor,
                limit=20,
            )
            transient_errors = 0
            messages = data.get("messages", [])

            # API trả mới nhất trước. Dùng seq để phòng trường hợp thứ tự thay đổi.
            messages = sorted(
                (m for m in messages if isinstance(m, dict)),
                key=lambda m: int(m.get("seq") or 0),
                reverse=True,
            )
            candidates = [m for m in messages if str(m.get("otp") or "").isdigit()]
            # Loc theo seq co the bo lo tin den truoc luc chot moc. Vi vay chap
            # nhan them moi tin den sau thoi diem so duoc cap cho account nay.
            if after_time:
                extra = [
                    m for m in messages
                    if str(m.get("otp") or "").isdigit()
                    and str(m.get("received_at") or "") >= after_time
                    and m not in candidates
                ]
                if extra:
                    log.info("📨 Nhận thêm %s tin theo mốc thời gian (%s)", len(extra), after_time)
                    candidates = extra + candidates
            preferred = [m for m in candidates if _looks_like_target(m)]
            selected = (preferred or candidates)[0] if (preferred or candidates) else None

            if selected:
                otp = str(selected["otp"]).strip()
                seq = int(selected.get("seq") or cursor)
                service = selected.get("service") or "chưa nhận diện"
                log.info(
                    f"✅ OTP Web nhận được mã mới cho {phone}: {otp} "
                    f"(seq={seq}, service={service})"
                )
                return otp

            returned_cursor = data.get("last_seq")
            if str(returned_cursor or "").isdigit():
                cursor = max(cursor, int(returned_cursor))
        except OTPWebError as exc:
            transient_errors += 1
            # Lỗi quyền/key không tự khỏi nếu cứ gọi lại, dừng ngay để fallback.
            error_text = str(exc)
            if "API key" in error_text or "chưa được cấp quyền" in error_text:
                raise
            if transient_errors == 1 or transient_errors % 5 == 0:
                log.warning(f"⚠️ OTP Web tạm thời lỗi ({transient_errors}): {exc}")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_interval, remaining))

    raise OTPWebTimeout(
        f"Không nhận được OTP mới từ OTP Web cho số {phone} "
        f"sau {int(timeout or config.OTP_WEB_TIMEOUT)} giây"
    )
