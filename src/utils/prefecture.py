"""Map proxy geolocation data to Japanese prefecture names."""

from __future__ import annotations

import re
import unicodedata


PREFECTURES = (
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県", "茨城県",
    "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県", "新潟県", "富山県",
    "石川県", "福井県", "山梨県", "長野県", "岐阜県", "静岡県", "愛知県", "三重県",
    "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県", "鳥取県", "島根県",
    "岡山県", "広島県", "山口県", "徳島県", "香川県", "愛媛県", "高知県", "福岡県",
    "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
)

_ROMAJI = (
    "hokkaido", "aomori", "iwate", "miyagi", "akita", "yamagata", "fukushima",
    "ibaraki", "tochigi", "gunma", "saitama", "chiba", "tokyo", "kanagawa",
    "niigata", "toyama", "ishikawa", "fukui", "yamanashi", "nagano", "gifu",
    "shizuoka", "aichi", "mie", "shiga", "kyoto", "osaka", "hyogo", "nara",
    "wakayama", "tottori", "shimane", "okayama", "hiroshima", "yamaguchi",
    "tokushima", "kagawa", "ehime", "kochi", "fukuoka", "saga", "nagasaki",
    "kumamoto", "oita", "miyazaki", "kagoshima", "okinawa",
)


def _normalise(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char)).lower()
    return re.sub(r"[^a-z0-9]", "", text)


_REGION_MAP = {}
for number, (romaji, japanese) in enumerate(zip(_ROMAJI, PREFECTURES), start=1):
    _REGION_MAP[_normalise(romaji)] = japanese
    _REGION_MAP[f"jp{number:02d}"] = japanese
    _REGION_MAP[f"{number:02d}"] = japanese

# Names commonly returned with an administrative suffix.
for suffix in ("prefecture", "ken", "fu", "to"):
    for romaji, japanese in zip(_ROMAJI, PREFECTURES):
        _REGION_MAP[_normalise(f"{romaji} {suffix}")] = japanese

# Thủ phủ và các thành phố lớn thường có trong username geo-target của proxy.
# City chưa có trong bảng này sẽ rơi xuống geo API, không tự đoán prefecture.
_CITY_MAP = {
    "sapporo": "北海道", "aomori": "青森県", "morioka": "岩手県",
    "sendai": "宮城県", "akita": "秋田県", "yamagata": "山形県",
    "fukushima": "福島県", "mito": "茨城県", "utsunomiya": "栃木県",
    "maebashi": "群馬県", "takasaki": "群馬県", "saitama": "埼玉県",
    "chiba": "千葉県", "tokyo": "東京都", "yokohama": "神奈川県",
    "kawasaki": "神奈川県", "sagamihara": "神奈川県", "niigata": "新潟県",
    "toyama": "富山県", "kanazawa": "石川県", "fukui": "福井県",
    "kofu": "山梨県", "nagano": "長野県", "gifu": "岐阜県",
    "shizuoka": "静岡県", "hamamatsu": "静岡県", "nagoya": "愛知県",
    "toyota": "愛知県", "tsu": "三重県", "otsu": "滋賀県",
    "kyoto": "京都府", "osaka": "大阪府", "sakai": "大阪府",
    "kobe": "兵庫県", "himeji": "兵庫県", "nara": "奈良県",
    "wakayama": "和歌山県", "tottori": "鳥取県", "matsue": "島根県",
    "okayama": "岡山県", "kurashiki": "岡山県", "hiroshima": "広島県",
    "fukuyama": "広島県", "yamaguchi": "山口県", "shimonoseki": "山口県",
    "tokushima": "徳島県", "takamatsu": "香川県", "matsuyama": "愛媛県",
    "kochi": "高知県", "fukuoka": "福岡県", "kitakyushu": "福岡県",
    "saga": "佐賀県", "nagasaki": "長崎県", "kumamoto": "熊本県",
    "oita": "大分県", "miyazaki": "宮崎県", "kagoshima": "鹿児島県",
    "naha": "沖縄県", "okinawa": "沖縄県",
}


def map_proxy_region(region: object = "", region_code: object = "") -> str | None:
    """Return a supported Japanese prefecture, or ``None`` when no map exists."""
    region_text = str(region or "").strip()
    if region_text in PREFECTURES:
        return region_text

    for candidate in (region_text, region_code):
        mapped = _REGION_MAP.get(_normalise(candidate))
        if mapped:
            return mapped
    return None


def valid_fallback(sheet_value: object, default_value: object) -> tuple[str, str]:
    """Choose a valid fallback and report which source supplied it."""
    sheet_prefecture = str(sheet_value or "").strip()
    if sheet_prefecture in PREFECTURES:
        return sheet_prefecture, "Excel"
    default_prefecture = str(default_value or "").strip()
    if default_prefecture in PREFECTURES:
        return default_prefecture, "cấu hình mặc định"
    return "愛知県", "fallback hệ thống"


def prefecture_from_proxy_metadata(proxy: dict | None) -> tuple[str | None, str]:
    """Read ByteProxies-style ``cc-jp-city-kobe`` targeting metadata."""
    if not proxy:
        return None, "proxy không có metadata"

    username = str(proxy.get("username") or "").strip().lower().replace("_", "-")
    if not username:
        return None, "proxy không có username geo-target"

    country_match = re.search(r"(?:^|-)cc-([a-z]{2})(?:-|$)", username)
    if country_match and country_match.group(1) != "jp":
        return None, f"metadata proxy không ở Nhật (cc={country_match.group(1)})"
    if not country_match:
        return None, "username proxy không có cc-jp"

    # Nếu provider ghi thẳng prefecture/state thì map trực tiếp.
    for prefix in ("prefecture", "state", "region"):
        for romaji, japanese in zip(_ROMAJI, PREFECTURES):
            if re.search(rf"(?:^|-){prefix}-{re.escape(romaji)}(?:-|$)", username):
                return japanese, f"metadata {prefix}-{romaji}"

    for city in sorted(_CITY_MAP, key=len, reverse=True):
        if re.search(rf"(?:^|-)city-{re.escape(city)}(?:-|$)", username):
            return _CITY_MAP[city], f"metadata city-{city}"

    city_match = re.search(r"(?:^|-)city-([a-z0-9]+)(?:-|$)", username)
    if city_match:
        return None, f"city-{city_match.group(1)} chưa có trong bảng map"
    return None, "username proxy không có city/state/prefecture"


def detect_proxy_prefecture(requests_session, proxies: dict, timeout: int = 10) -> tuple[str | None, str]:
    """Resolve the current proxy exit node via ipwho.is without breaking the run."""
    try:
        response = requests_session.get(
            "https://ipwho.is/",
            proxies=proxies,
            timeout=timeout,
            verify=False,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("success") is False:
            return None, str(payload.get("message") or "geo API trả success=false")

        country_code = str(payload.get("country_code") or "").upper()
        if country_code != "JP":
            return None, f"IP proxy không ở Nhật (country={country_code or 'unknown'})"

        region = payload.get("region") or ""
        region_code = payload.get("region_code") or ""
        prefecture = map_proxy_region(region, region_code)
        if not prefecture:
            return None, f"không map được region={region!r}, region_code={region_code!r}"
        return prefecture, f"region={region!r}, region_code={region_code!r}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
