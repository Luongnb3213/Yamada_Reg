"""Shared proxy parsing, grouping and health checks for runtime code."""

from __future__ import annotations

import time
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from src import config
from src.utils.prefecture import map_proxy_region, prefecture_from_proxy_metadata


GEO_URL = "https://ipwho.is/"


def requests_proxy_url(proxy: dict) -> str:
    """Build a requests-compatible authenticated proxy URL."""
    server = str(proxy.get("server") or "").strip()
    if not server:
        raise ValueError("Proxy thiếu server")
    if "://" not in server:
        server = "http://" + server
    parsed = urlsplit(server)
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    username = str(proxy.get("username") or "")
    password = str(proxy.get("password") or "")
    auth = ""
    if username:
        auth = quote(username, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        auth += "@"
    return urlunsplit((parsed.scheme or "http", auth + hostname + port, "", "", ""))


def requests_proxy_dict(proxy: dict) -> dict[str, str]:
    url = requests_proxy_url(proxy)
    return {"http": url, "https": url}


def proxy_group_key(proxy: dict) -> str:
    """Group gateway rows sharing the same provider account/quota pool."""
    server = str(proxy.get("server") or "").strip().lower()
    username = str(proxy.get("username") or "").strip().lower()
    # ByteProxies appends geo/session selectors after the base pool name.
    selector_positions = [
        position for token in (
            "-cc-", "-country-", "-city-", "-state-", "-region-",
            "-session-", "-sessid-", "-sticky-",
        )
        if (position := username.find(token)) >= 0
    ]
    pool_name = username[:min(selector_positions)] if selector_positions else username
    # Password participates in identity but is never exposed in the label/log.
    password = str(proxy.get("password") or "")
    return "\x1f".join((server, pool_name, password))


def proxy_group_label(proxy: dict) -> str:
    server = str(proxy.get("server") or "").replace("http://", "").replace("https://", "")
    username = str(proxy.get("username") or "").strip().lower()
    positions = [
        position for token in ("-cc-", "-country-", "-city-", "-state-", "-region-")
        if (position := username.find(token)) >= 0
    ]
    pool_name = username[:min(positions)] if positions else username
    return f"{server} | {pool_name or 'no-auth'}"


def is_quota_or_auth_error(detail: str, http_status: int | None = None) -> bool:
    text = str(detail or "").lower()
    return http_status in (401, 402, 407, 429) or any(token in text for token in (
        "407", "proxy authentication", "authentication required", "quota",
        "bandwidth", "traffic limit", "insufficient balance", "account disabled",
    ))


def check_proxy(proxy: dict, timeout: int = 10, include_geo: bool = False, url: str | None = None) -> dict:
    """Check target-site access through one proxy without consuming it."""
    started = time.monotonic()
    result = {
        "ok": False,
        "status": "ERROR",
        "latency_ms": 0,
        "ip": "",
        "prefecture": "",
        "detail": "",
        "group_key": proxy_group_key(proxy),
        "group": proxy_group_label(proxy),
        "requests_proxies": {},
        "fatal_group": False,
    }
    try:
        proxies = requests_proxy_dict(proxy)
        result["requests_proxies"] = proxies
        health_url = str(url or getattr(config, "PROXY_HEALTH_URL", "") or "https://example.com/")
        response = requests.get(
            health_url,
            proxies=proxies,
            timeout=timeout,
            verify=False,
        )
        body = response.text or ""
        if "アクセス集中" in body or "too many requests" in body.lower():
            result.update(status="SITE_OVERLOADED", detail="Target site đang quá tải; không phải lỗi proxy")
            return result
        if (
            "Access Denied" in body
            or "access denied" in body.lower()
            or "エラーが発生しました" in body
        ):
            result.update(status="IP_BANNED", detail="IP proxy bị target site từ chối")
            return result
        response.raise_for_status()

        prefecture, metadata_detail = prefecture_from_proxy_metadata(proxy)
        result.update(ok=True, status="OK", prefecture=prefecture or "", detail=metadata_detail)

        if include_geo:
            try:
                geo_response = requests.get(
                    GEO_URL,
                    proxies=proxies,
                    timeout=timeout,
                    verify=False,
                )
                geo_response.raise_for_status()
                payload = geo_response.json()
                if payload.get("success") is not False:
                    result["ip"] = str(payload.get("ip") or "")
                    country = str(payload.get("country_code") or "").upper()
                    mapped = map_proxy_region(payload.get("region"), payload.get("region_code"))
                    if country == "JP" and mapped:
                        result["prefecture"] = mapped
                        result["detail"] = f"Geo thực tế: {payload.get('region') or payload.get('region_code')}"
                    elif country and country != "JP":
                        result["detail"] = f"Cảnh báo: IP thoát ở {country}, không phải JP"
            except Exception as geo_error:
                result["detail"] += f"; không đọc được geo: {type(geo_error).__name__}"
        return result
    except Exception as exc:
        http_status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"{type(exc).__name__}: {exc}"
        result.update(
            status="QUOTA/AUTH" if is_quota_or_auth_error(detail, http_status) else "DEAD",
            detail=detail,
            fatal_group=is_quota_or_auth_error(detail, http_status),
        )
        return result
    finally:
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
