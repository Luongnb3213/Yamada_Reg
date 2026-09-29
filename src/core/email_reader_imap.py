from __future__ import annotations

import email
import html
import imaplib
import re
import time
import unicodedata
from datetime import datetime
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
    search_expr = f'(FROM "{senders[0]}")' if len(senders) == 1 else "ALL"
    deadline = time.time() + timeout
    log.info("[%s] Chờ OTP từ %s qua %s, quét tất cả mailbox.", target_email, inbox, imap_server)
    seen_messages: set[str] = set()

    while time.time() < deadline:
        try:
            mail = imaplib.IMAP4_SSL(imap_server)
            mail.login(inbox, password)
            mailboxes = _list_mailboxes(mail)
            for mailbox in mailboxes:
                try:
                    selected, _ = mail.select(f'"{mailbox}"', readonly=True)
                    if selected != "OK":
                        continue
                except Exception:
                    continue
                status, messages = mail.search(None, search_expr)
                if status != "OK" or not messages or not messages[0]:
                    continue
                for num in reversed(messages[0].split()[-50:]):
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
                    if since_ts and _message_ts(msg) < since_ts:
                        continue
                    if not _sender_matches(from_filter, msg):
                        continue
                    body = _body_text(msg)
                    if not _target_matches(target_email, msg, body):
                        continue
                    code = _extract_code(body, code_pattern=code_pattern)
                    if code:
                        mail.logout()
                        log.info("[%s] Đã tìm thấy OTP qua IMAP trong mailbox %s.", target_email, mailbox)
                        return code
            mail.logout()
        except imaplib.IMAP4.error as exc:
            log.error("[%s] Lỗi đăng nhập IMAP %s: %s", target_email, inbox, exc)
            return ""
        except Exception as exc:
            log.debug("[%s] Lỗi đọc IMAP tạm thời: %s", target_email, exc)
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
