"""OTPBase /api/v1/otp client."""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import src.config as config
from src.utils.logger import get_logger


OTPBASE_OTP_URL = "https://www.otpbase.space/api/v1/otp"
log = get_logger("otpbase_client")


class OTPBaseError(RuntimeError):
    """Actionable OTPBase API error."""


class OTPBaseTemporaryError(OTPBaseError):
    """Network/server error that polling may retry."""


def normalize_phone(value: str) -> str:
    number = re.sub(r"\D", "", str(value or ""))
    if number.startswith("81") and len(number) == 12:
        number = "0" + number[2:]
    return number


def _seq(value: object) -> int:
    return int(str(value)) if str(value).isascii() and str(value).isdigit() else -1


def _message_phone(message: dict) -> str:
    for key in ("phone", "tel", "number", "to", "recipient"):
        value = str(message.get(key) or "").strip()
        if value:
            return value
    return ""


def _message_content(message: dict) -> str:
    for key in ("content", "message", "sms", "text", "body"):
        value = str(message.get(key) or "")
        if value:
            return unicodedata.normalize("NFKC", value)
    nested = message.get("data")
    if isinstance(nested, dict):
        return _message_content(nested)
    return ""


def _message_source(message: dict) -> str:
    values = []
    for key in ("service", "sender", "source", "from", "provider", "label"):
        value = str(message.get(key) or "").strip()
        if value:
            values.append(f"{key}={value}")
    nested = message.get("data")
    if isinstance(nested, dict):
        nested_source = _message_source(nested)
        if nested_source:
            values.append(nested_source)
    return ", ".join(values)


def _looks_like_target(message: dict) -> bool:
    source_keywords = tuple(getattr(config, "OTP_SOURCE_KEYWORDS", ()) or ())
    if not source_keywords:
        return False
    haystack = " ".join(
        str(message.get(key) or "")
        for key in ("service", "sender", "source", "from", "provider", "label", "content", "message", "sms", "text", "body")
    )
    nested = message.get("data")
    if isinstance(nested, dict):
        haystack += " " + " ".join(str(v or "") for v in nested.values())
    normalized = unicodedata.normalize("NFKC", haystack).casefold()
    return any(str(keyword).casefold() in normalized for keyword in source_keywords)


def _code_from_content(content: str) -> str:
    text = unicodedata.normalize("NFKC", str(content or ""))
    preferred = re.search(
        r"(?:認証コード|verification code|verify code|code)\D{0,16}([0-9]{4,8})(?![0-9])",
        text,
        re.I,
    )
    if preferred:
        return preferred.group(1)
    generic = re.search(r"(?<![0-9])([0-9]{4,8})(?![0-9])", text)
    return generic.group(1) if generic else ""


@dataclass(frozen=True)
class OTPBaseCursor:
    seq: int = -1


class OTPBaseClient:
    def __init__(
        self,
        api_key: str,
        *,
        cancel: threading.Event | None = None,
        timeout: float = 120.0,
        poll_interval: float = 2.0,
    ) -> None:
        self._api_key = api_key.strip()
        if not self._api_key:
            raise OTPBaseError("Chưa có API key OTPBase.")
        self.cancel = cancel if cancel is not None else threading.Event()
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.last_otp_source = ""
        self.last_message_source = ""
        self.last_source_matched = False

    def _check_cancelled(self) -> None:
        if self.cancel.is_set() or getattr(config, "STOP_FLAG", False):
            raise OTPBaseError("Đã dừng chờ OTP theo yêu cầu người dùng.")

    def _request(self, phone: str, *, request_timeout: float = 15.0, **params: int | str) -> dict:
        self._check_cancelled()
        query = urlencode({"phone": normalize_phone(phone), **params})
        req = Request(f"{OTPBASE_OTP_URL}?{query}", headers={"x-api-key": self._api_key})
        try:
            with urlopen(req, timeout=request_timeout) as response:
                data = json.load(response)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 401:
                raise OTPBaseError("API key OTPBase không đúng hoặc đã bị thu hồi.") from None
            if status == 403:
                raise OTPBaseError("API key OTPBase chưa được cấp quyền cho số điện thoại này.") from None
            error = OTPBaseTemporaryError if status == 429 or status >= 500 else OTPBaseError
            raise error(f"OTPBase trả HTTP {status}.") from None
        except (URLError, TimeoutError, OSError):
            raise OTPBaseTemporaryError("Không kết nối được OTPBase.") from None
        except (ValueError, UnicodeError):
            raise OTPBaseError("OTPBase trả dữ liệu JSON không hợp lệ.") from None

        self._check_cancelled()
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise OTPBaseError("OTPBase không chấp nhận yêu cầu.")
        if "found" not in data:
            raise OTPBaseError("OTPBase /otp không trả field `found` hợp lệ.")
        return data

    def capture_cursor(self, phone: str) -> OTPBaseCursor:
        data = self._request(phone)
        return OTPBaseCursor(seq=max(_seq(data.get("seq")), _seq(data.get("last_seq"))))

    def _code_from_response(self, data: dict, phone: str, cursor: OTPBaseCursor) -> str:
        self.last_otp_source = ""
        self.last_message_source = ""
        self.last_source_matched = False
        if data.get("found") is not True:
            return ""

        seq = max(_seq(data.get("seq")), _seq(data.get("last_seq")))
        if cursor.seq >= 0 and seq <= cursor.seq:
            return ""
        if normalize_phone(_message_phone(data)) != normalize_phone(phone):
            return ""

        self.last_message_source = _message_source(data)
        self.last_source_matched = _looks_like_target(data)
        if self.last_message_source:
            log.info(
                "📨 OTPBase source cho %s: %s%s",
                normalize_phone(phone),
                self.last_message_source,
                " (match app)" if self.last_source_matched else " (chưa match keyword app)",
            )

        otp = re.sub(r"\D", "", str(data.get("otp") or ""))
        if 4 <= len(otp) <= 8:
            self.last_otp_source = "otp"
            return otp

        code = _code_from_content(_message_content(data))
        if code:
            self.last_otp_source = "message"
            return code
        return ""

    def wait_for_otp(
        self,
        phone: str,
        after_seq: OTPBaseCursor | int,
        *,
        timeout: float | None = None,
    ) -> str:
        cursor = after_seq if isinstance(after_seq, OTPBaseCursor) else OTPBaseCursor(seq=int(after_seq))
        wait_timeout = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + wait_timeout
        last_error = ""

        while (remaining := deadline - time.monotonic()) > 0:
            self._check_cancelled()
            try:
                params: dict[str, int] = {}
                if cursor.seq >= 0:
                    params["after_seq"] = cursor.seq
                data = self._request(phone, request_timeout=min(15.0, remaining), **params)
                last_error = ""
                code = self._code_from_response(data, phone, cursor)
                if code:
                    return code
            except OTPBaseTemporaryError as exc:
                last_error = str(exc)
            if self.cancel.wait(min(self.poll_interval, max(0, deadline - time.monotonic()))):
                self._check_cancelled()

        raise OTPBaseError(
            f"Hết {wait_timeout:g}s chờ OTP mới cho số điện thoại."
            + (f" {last_error}" if last_error else "")
        )
