from __future__ import annotations

import email
import html
import imaplib
import re
import time
import unicodedata
from datetime import datetime, timedelta
from email.message import Message
from imaplib import IMAP4

from src.utils.logger import get_logger


log = get_logger("email_reader_imap")


_IMAP_HOSTS = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "icloud.com": "imap.mail.me.com",
    "me.com": "imap.mail.me.com",
    "mac.com": "imap.mail.me.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com",
    "msn.com": "outlook.office365.com",
    "outlook.jp": "outlook.office365.com",
    "hotmail.co.jp": "outlook.office365.com",
    "yahoo.com": "imap.mail.yahoo.com",
    "aol.com": "imap.aol.com",
    "zoho.com": "imap.zoho.com",
    "gmx.com": "imap.gmx.com",
    "yandex.com": "imap.yandex.com",
    "mail.ru": "imap.mail.ru",
}


def _get_imap_server(email_address: str, imap_host: str = "") -> str:
    import src.config as config

    if imap_host:
        return imap_host
    override = str(getattr(config, "IMAP_HOST", "") or "").strip()
    if override:
        return override
    domain = email_address.split("@")[-1].lower()
    return _IMAP_HOSTS.get(domain, "")


def _message_ts(msg: Message) -> float:
    date_tuple = email.utils.parsedate_tz(msg.get("Date"))
    if not date_tuple:
        return 0
    return datetime.fromtimestamp(email.utils.mktime_tz(date_tuple)).timestamp()


def _body_text(msg: Message) -> str:
    chunks = []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            raw = str(part.get_payload() or "")
        else:
            charset = part.get_content_charset() or "utf-8"
            raw = payload.decode(charset, errors="replace")
        if part.get_content_type() == "text/html":
            raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
            raw = re.sub(r"(?s)<[^>]+>", " ", raw)
            raw = html.unescape(raw)
        chunks.append(raw)
    return "\n".join(chunks)


def _target_matches(target_email: str, msg: Message, body: str) -> bool:
    target = str(target_email or "").strip().lower()
    if not target:
        return True
    haystack = " ".join(
        str(msg.get(header, "") or "").lower()
        for header in ("To", "Delivered-To", "Cc")
    )
    return target in haystack or target in body.lower()


def _sender_matches(from_filter: str, msg: Message) -> bool:
    senders = [
        item.strip().lower()
        for item in re.split(r"[,;]", str(from_filter or ""))
        if item.strip()
    ]
    if not senders:
        return True
    sender = str(msg.get("From", "") or "").lower()
    sender_variants = {
        sender,
        sender.replace("_at_", "@"),
        sender.replace("_at_", "_"),
    }
    sender_relaxed = re.sub(r"[^a-z0-9]+", "", sender.replace("_at_", "_"))
    for item in senders:
        if any(item in variant for variant in sender_variants):
            return True
        item_relaxed = re.sub(r"[^a-z0-9]+", "", item)
        if item_relaxed and item_relaxed in sender_relaxed:
            return True
    return False


def _extract_code(text: str, code_pattern: str = "") -> str:
    text = unicodedata.normalize("NFKC", html.unescape(str(text or "")))
    if code_pattern:
        match = re.search(code_pattern, text, re.I)
        if match:
            value = match.group(1) if match.lastindex else match.group(0)
            code = re.sub(r"\D", "", value)
            if 4 <= len(code) <= 8:
                return code

    preferred_patterns = [
        r"(?:認証コード|認証番号|確認コード|ワンタイムパスワード|verification code|verify code|confirmation code|security code|one[-\s]?time password|code)\D{0,80}([0-9]{4,8})(?![0-9])",
        r"(?<![0-9])([0-9]{4,8})(?![0-9])\D{0,40}(?:を入力|をご入力|is your|verification code|confirmation code)",
    ]
    for pattern in preferred_patterns:
        for match in re.finditer(pattern, text, re.I):
            return match.group(1)

    # Yamada also sends completion mails containing a member number. Do not
    # treat that number as an email OTP.
    if re.search(r"(会員番号|member\s*(?:number|id))", text, re.I):
        return ""

    for match in re.finditer(r"(?<![0-9])([0-9]{4,8})(?![0-9])", text):
        before = text[max(0, match.start() - 40):match.start()]
        after = text[match.end():match.end() + 40]
        context = before + after
        if re.search(r"(会員番号|電話番号|郵便番号|生年月日|member\s*(?:number|id)|phone|postal|birthday)", context, re.I):
            continue
        return match.group(1)
    return ""


def _resolve_inbox(target_email: str, otp_email: str, otp_pass: str) -> tuple[str, str]:
    import src.config as config

    inbox = str(otp_email or "").strip() or str(getattr(config, "CATCHALL_INBOX", "") or "").strip()
    password = str(otp_pass or "").strip() or str(getattr(config, "CATCHALL_PASSWORD", "") or "").strip()
    if not inbox:
        inbox = target_email
    return inbox, password


def _decode_mailbox_name(raw: bytes | str) -> str:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")
    match = re.search(r' (?:"([^"]+)"|(\S+))$', text)
    name = (match.group(1) or match.group(2)) if match else text.rsplit(" ", 1)[-1]
    return name.strip('"')


def _mailbox_priority(name: str) -> tuple[int, str]:
    lower = name.lower()
    if lower in ("inbox",):
        return (0, lower)
    if "spam" in lower or "junk" in lower:
        return (1, lower)
    if "all mail" in lower or "allmail" in lower or "すべて" in lower:
        return (2, lower)
    return (3, lower)


def _scan_mailboxes(names: list[str]) -> list[str]:
    import src.config as config

    include_all = bool(getattr(config, "EMAIL_IMAP_INCLUDE_ALL_MAIL", False))
    if include_all:
        return names
    primary = [name for name in names if _mailbox_priority(name)[0] <= 1]
    return primary or names[:1]


def _list_mailboxes(mail: IMAP4) -> list[str]:
    names = ["INBOX"]
    try:
        status, boxes = mail.list()
        if status == "OK":
            for item in boxes or []:
                name = _decode_mailbox_name(item)
                if name and name not in names:
                    names.append(name)
    except Exception as exc:
        log.debug("Không list được mailbox IMAP: %s", exc)
    return sorted(names, key=_mailbox_priority)


def _message_key(mailbox: str, num: bytes, msg: Message) -> str:
    msg_id = str(msg.get("Message-ID") or msg.get("Message-Id") or "").strip()
    if msg_id:
        return msg_id
    return f"{mailbox}:{num.decode(errors='replace') if isinstance(num, bytes) else num}"


def _imap_date(ts: float) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%d-%b-%Y")


def _gmail_raw_date(ts: float) -> str:
    if not ts:
        return ""
    # Gmail's X-GM-RAW newer:/after: date handling is day-granular and can
    # exclude messages from the same local day. Search from the previous day,
    # then keep the strict second-level cutoff with the message Date header.
    return (datetime.fromtimestamp(ts) - timedelta(days=1)).strftime("%Y/%m/%d")


def _quote_search_value(value: str) -> str:
    escaped = str(value or "").replace("\\", "\\\\").replace('"', r"\"")
    return f'"{escaped}"'


def _sender_search_criteria(senders: list[str]) -> list[str]:
    if not senders:
        return []
    if len(senders) == 1:
        return ["FROM", _quote_search_value(senders[0])]
    criteria = ["OR", "FROM", _quote_search_value(senders[0]), "FROM", _quote_search_value(senders[1])]
    for sender in senders[2:]:
        criteria = ["OR", "FROM", _quote_search_value(sender), *criteria]
    return criteria


def _search_attempts(since_ts: float, senders: list[str]) -> list[list[str]]:
    import src.config as config

    since_date = _imap_date(since_ts)
    sender_criteria = _sender_search_criteria(senders)
    broad_fallback = bool(getattr(config, "EMAIL_IMAP_BROAD_FALLBACK", False))
    attempts: list[list[str]] = []
    if since_date and sender_criteria:
        attempts.append(["SINCE", since_date, *sender_criteria])
    if since_date and (not sender_criteria or broad_fallback):
        attempts.append(["SINCE", since_date])
    if not since_date and sender_criteria:
        attempts.append(sender_criteria)
    if not since_date and (not sender_criteria or broad_fallback):
        attempts.append(["ALL"])

    unique: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for attempt in attempts:
        key = tuple(attempt)
        if key not in seen:
            seen.add(key)
            unique.append(attempt)
    return unique


def _search_messages(mail: IMAP4, since_ts: float, senders: list[str]) -> tuple[str, list[bytes]]:
    for criteria in _search_attempts(since_ts, senders):
        try:
            status, messages = mail.search(None, *criteria)
        except Exception as exc:
            log.debug("IMAP search lỗi với criteria=%s: %s", criteria, exc)
            continue
        if status == "OK" and messages and messages[0]:
            return status, messages[0].split()
    return "OK", []


def _gmail_raw_search_messages(mail: IMAP4, target_email: str, since_ts: float) -> list[bytes]:
    target = str(target_email or "").strip()
    if not target:
        return []
    parts = []
    newer = _gmail_raw_date(since_ts)
    if newer:
        parts.append(f"newer:{newer}")
    parts.append(target)
    query = " ".join(parts)
    try:
        status, messages = mail.uid("SEARCH", None, "X-GM-RAW", f'"{query}"')
    except Exception as exc:
        log.debug("[%s] Gmail raw search lỗi: %s", target, exc)
        return []
    if status != "OK" or not messages or not messages[0]:
        return []
    return messages[0].split()


def get_email_otp_imap(
    target_email: str,
    otp_email: str = "",
    otp_pass: str = "",
    timeout: int = 120,
    since_ts: float = 0,
    from_filter: str = "",
    code_pattern: str = "",
    poll_interval: int = 5,
    imap_host: str = "",
) -> str:
    """Poll an IMAP inbox and return a fresh numeric OTP code."""
    inbox, password = _resolve_inbox(target_email, otp_email, otp_pass)
    if not inbox or not password:
        log.error("[%s] Thiếu inbox/app password để đọc OTP.", target_email)
        return ""

    imap_server = _get_imap_server(inbox, imap_host=imap_host)
    if not imap_server:
        domain = inbox.split("@")[-1].lower()
        log.error("[%s] Không biết IMAP host cho domain %s. Hãy cấu hình imap_host.", target_email, domain)
        return ""

    senders = [
        item.strip()
        for item in re.split(r"[,;]", str(from_filter or ""))
        if item.strip()
    ]
    deadline = time.time() + timeout
    seen_messages: set[str] = set()
    import src.config as config
    socket_timeout = int(getattr(config, "EMAIL_IMAP_SOCKET_TIMEOUT", 20) or 20)
    max_messages = int(getattr(config, "EMAIL_IMAP_MAX_MESSAGES", 80) or 80)
    log.info(
        "[%s] Chờ OTP từ %s qua %s, timeout=%ss, socket_timeout=%ss, max_messages=%s.",
        target_email,
        inbox,
        imap_server,
        timeout,
        socket_timeout,
        max_messages,
    )

    while time.time() < deadline:
        mail = None
        try:
            mail = imaplib.IMAP4_SSL(imap_server, timeout=socket_timeout)
            mail.login(inbox, password)
            mailboxes = _scan_mailboxes(_list_mailboxes(mail))
            candidates: list[tuple[float, int, str, str]] = []
            for mailbox in mailboxes:
                if time.time() >= deadline:
                    break
                try:
                    selected, _ = mail.select(f'"{mailbox}"', readonly=True)
                    if selected != "OK":
                        continue
                except Exception:
                    continue
                use_uid_fetch = False
                message_nums = _gmail_raw_search_messages(mail, target_email, since_ts)
                if message_nums:
                    use_uid_fetch = True
                if not message_nums:
                    status, message_nums = _search_messages(mail, since_ts, senders)
                    if status != "OK":
                        continue
                if not message_nums:
                    continue
                for num in reversed(message_nums[-max_messages:]):
                    if time.time() >= deadline:
                        break
                    if use_uid_fetch:
                        res, msg_data = mail.uid("FETCH", num, "(BODY.PEEK[])")
                    else:
                        res, msg_data = mail.fetch(num, "(BODY.PEEK[])")
                    if res != "OK":
                        continue
                    raw = next((part[1] for part in msg_data if isinstance(part, tuple)), None)
                    if not raw:
                        continue
                    msg = email.message_from_bytes(raw)
                    key = _message_key(mailbox, num, msg)
                    if key in seen_messages:
                        continue
                    seen_messages.add(key)
                    msg_ts = _message_ts(msg)
                    if since_ts and msg_ts < since_ts:
                        continue
                    if not _sender_matches(from_filter, msg):
                        continue
                    body = _body_text(msg)
                    if not _target_matches(target_email, msg, body):
                        continue
                    code = _extract_code(body, code_pattern=code_pattern)
                    if code:
                        try:
                            seq = int(num)
                        except (TypeError, ValueError):
                            seq = 0
                        candidates.append((msg_ts, seq, mailbox, code))
            try:
                mail.logout()
            except Exception:
                pass
            if candidates:
                candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
                _, _, mailbox, code = candidates[0]
                log.info("[%s] Đã tìm thấy OTP mới nhất qua IMAP trong mailbox %s.", target_email, mailbox)
                return code
        except imaplib.IMAP4.error as exc:
            log.error("[%s] Lỗi đăng nhập IMAP %s: %s", target_email, inbox, exc)
            return ""
        except Exception as exc:
            log.debug("[%s] Lỗi đọc IMAP tạm thời: %s", target_email, exc)
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    pass
        time.sleep(poll_interval)

    log.warning("[%s] Hết thời gian chờ OTP qua IMAP.", target_email)
    return ""


def get_gmail_dot_alias(base_email: str, index: int) -> str:
    username, domain = base_email.split("@", 1)
    if domain.lower() not in ("gmail.com", "googlemail.com"):
        return base_email
    gaps = len(username) - 1
    if gaps <= 0:
        return base_email
    binary_str = bin(index % (2**gaps))[2:].zfill(gaps)
    result = []
    for pos, char in enumerate(username):
        result.append(char)
        if pos < gaps and binary_str[pos] == "1":
            result.append(".")
    return "".join(result) + "@" + domain


def generate_account_email(account_id: int | str) -> str:
    import src.config as config

    prefix = getattr(config, "CATCHALL_EMAIL_PREFIX", "acc")
    suffix = f"{account_id:05d}" if isinstance(account_id, int) else str(account_id)
    if getattr(config, "EMAIL_MODE", "alias") == "alias":
        catchall_inbox = getattr(config, "CATCHALL_INBOX", "")
        if not catchall_inbox:
            raise ValueError("CATCHALL_INBOX chưa được cấu hình.")
        base, domain = catchall_inbox.split("@", 1)
        if domain.lower() in ("gmail.com", "googlemail.com"):
            try:
                idx = int(suffix)
            except ValueError:
                idx = abs(hash(suffix))
            return get_gmail_dot_alias(catchall_inbox, idx)
        return f"{base}+{prefix}{suffix}@{domain}"

    catchall_domain = getattr(config, "CATCHALL_DOMAIN", "")
    if not catchall_domain:
        raise ValueError("CATCHALL_DOMAIN chưa được cấu hình.")
    return f"{prefix}{suffix}@{catchall_domain}"
