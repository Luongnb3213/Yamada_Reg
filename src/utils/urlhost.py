"""So sánh host/path của URL cho đúng, tránh match nhầm query redirect."""

from __future__ import annotations

from urllib.parse import urlparse


def host_of(url: object) -> str:
    """Trả về hostname (không port), viết thường."""
    try:
        return (urlparse(str(url or "")).hostname or "").lower()
    except Exception:
        return ""


def is_host(url: object, host: str) -> bool:
    """True khi URL đang thực sự ở ``host`` (hoặc subdomain của nó)."""
    target = str(host or "").lower().strip()
    if not target:
        return False
    current = host_of(url)
    return current == target or current.endswith("." + target)


def path_has(url: object, needle: str) -> bool:
    """True khi PHAN PATH cua URL chua ``needle``.

    Khong dung ``needle in url``: redirect URL co the nhet path dich vao query,
    nen kiem tra tren ca chuoi se khop nham.
    """
    try:
        return str(needle or "").lower() in (urlparse(str(url or "")).path or "").lower()
    except Exception:
        return False
