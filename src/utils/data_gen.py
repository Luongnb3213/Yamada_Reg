import calendar
import random
import hashlib
from datetime import date, datetime, timedelta
import src.config as config
from src.utils.logger import get_logger

log = get_logger("data_gen")

# Seed random using email to ensure consistency for retries
def get_seeded_random(seed_str: str) -> random.Random:
    hash_object = hashlib.md5(seed_str.encode("utf-8"))
    seed_int = int(hash_object.hexdigest(), 16)
    return random.Random(seed_int)


# ─── Ngày sinh ───────────────────────────────────────────────────────────────
# TRƯỚC ĐÂY: khi Excel không có cột dob, mọi account đều nhận đúng một ngày
# sinh từ config (`default_dob`), nên cả lô hàng trăm tài khoản khai trùng khít
# một ngày — một dấu vân tay quá rõ. Giờ mỗi account tự sinh một ngày riêng
# trong khoảng năm cấu hình được.

DOB_YEAR_MIN_DEFAULT = 1995
DOB_YEAR_MAX_DEFAULT = 2007


def dob_year_range() -> tuple[int, int]:
    """Khoảng năm sinh hợp lệ, lấy từ config và tự sửa nếu người dùng nhập ngược."""
    try:
        low = int(getattr(config, "DOB_YEAR_MIN", DOB_YEAR_MIN_DEFAULT))
    except (TypeError, ValueError):
        low = DOB_YEAR_MIN_DEFAULT
    try:
        high = int(getattr(config, "DOB_YEAR_MAX", DOB_YEAR_MAX_DEFAULT))
    except (TypeError, ValueError):
        high = DOB_YEAR_MAX_DEFAULT
    if low > high:
        low, high = high, low
    # Chặn trên theo năm hiện tại: năm sinh ở tương lai thì site từ chối ngay.
    this_year = date.today().year
    high = min(high, this_year)
    low = min(low, high)
    return low, high


def generate_birthday(email: str) -> str:
    """Sinh ngày sinh ngẫu nhiên trong khoảng năm cấu hình. Dạng YYYY-MM-DD.

    Gieo hạt theo email nên chạy lại cùng một account luôn ra đúng một ngày —
    cần thiết vì account thất bại giữa chừng sẽ được thử lại, và ngày sinh khai
    ở lần sau phải khớp lần trước.

    Chọn năm rồi cộng số ngày trong chính năm đó, thay vì bốc riêng tháng và
    ngày: cách sau đẻ ra 31/02 hay 29/02 của năm không nhuận.
    """
    low, high = dob_year_range()
    r = get_seeded_random(email)
    year = r.randint(low, high)
    days_in_year = 366 if calendar.isleap(year) else 365
    birth_date = date(year, 1, 1) + timedelta(days=r.randrange(days_in_year))
    return birth_date.strftime("%Y-%m-%d")


def normalize_birthday(value) -> str:
    """Đưa ngày sinh đọc từ Excel về đúng ``YYYY-MM-DD``; trả "" nếu không đọc được.

    Ô ngày trong Excel về tay ta dưới dạng ``datetime``, và khi bị ép sang chuỗi
    thì thành ``"1995-11-12 00:00:00"``. Các bước sau lại tách ngày bằng
    ``birthday.split("-")``, nên phần dư ``" 00:00:00"`` sẽ chui thẳng vào ô
    "ngày" của form.

    Chỉ nhận những định dạng KHÔNG mơ hồ. ``03/04/1995`` có thể là 3 tháng 4 hay
    4 tháng 3 — đoán sai là khai sai ngày sinh, nên thà trả "" để sinh ngẫu
    nhiên còn hơn.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")

    text = str(value).strip()
    if not text:
        return ""
    # Bỏ phần giờ nếu có ("1995-11-12 00:00:00" hoặc "1995-11-12T00:00:00").
    text = text.replace("T", " ").split(" ")[0]

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def resolve_birthday(email: str, raw_from_sheet=None) -> tuple[str, str]:
    """Chốt ngày sinh cho một account: ưu tiên Excel, không có thì sinh ngẫu nhiên.

    Trả về ``(YYYY-MM-DD, nguồn)`` với nguồn là ``"Excel"`` hoặc ``"ngẫu nhiên"``.
    """
    from_sheet = normalize_birthday(raw_from_sheet)
    if from_sheet:
        return from_sheet, "Excel"

    raw_text = str(raw_from_sheet or "").strip()
    if raw_text:
        low, high = dob_year_range()
        log.warning(
            "⚠️ Ngày sinh %r của %s không đọc được (chỉ nhận YYYY-MM-DD, "
            "YYYY/MM/DD, YYYYMMDD) — sinh ngẫu nhiên trong %s–%s thay thế.",
            raw_text, email, low, high,
        )
    return generate_birthday(email), "ngẫu nhiên"

def generate_nickname(email: str) -> str:
    """Sinh nickname ngẫu nhiên từ danh sách tên phổ biến."""
    r = get_seeded_random(email)
    first_names = [
        "Sato", "Suzuki", "Takahashi", "Tanaka", "Watanabe", "Ito", "Yamamoto",
        "Nakamura", "Kobayashi", "Kato", "Yoshida", "Yamada", "Sasaki", "Yamaguchi",
        "Saito", "Matsumoto", "Inoue", "Kimura", "Hayashi", "Shimizu", "Koji",
        "Hiro", "Ken", "Shin", "Taku", "Yuki", "Haru", "Ren", "Sho", "Taiga"
    ]
    suffixes = ["kun", "chan", "san", "99", "123", "parks", "bn", "jp", "88"]
    
    name = r.choice(first_names)
    suffix = r.choice(suffixes)
    return f"{name}{suffix}"

def generate_password(email: str) -> str:
    """Trả về mật khẩu mặc định chung từ cấu hình."""
    return config.DEFAULT_PASSWORD


# ─── Japanese profile data: kanji + katakana name ───

# (kanji, katakana) — họ và tên phổ biến, đọc chuẩn.
_JP_SURNAMES = [
    ("佐藤", "サトウ"), ("鈴木", "スズキ"), ("高橋", "タカハシ"),
    ("田中", "タナカ"), ("渡辺", "ワタナベ"), ("伊藤", "イトウ"),
    ("山本", "ヤマモト"), ("中村", "ナカムラ"), ("小林", "コバヤシ"),
    ("加藤", "カトウ"), ("吉田", "ヨシダ"), ("山田", "ヤマダ"),
    ("佐々木", "ササキ"), ("松本", "マツモト"), ("井上", "イノウエ"),
]

_JP_GIVEN_NAMES = [
    ("大輔", "ダイスケ"), ("健太", "ケンタ"), ("直樹", "ナオキ"),
    ("拓也", "タクヤ"), ("翔太", "ショウタ"), ("和也", "カズヤ"),
    ("智也", "トモヤ"), ("裕太", "ユウタ"), ("亮", "リョウ"),
    ("誠", "マコト"), ("学", "マナブ"), ("聡", "サトシ"),
]


def generate_jp_name(email: str) -> dict:
    """Sinh họ tên Nhật (kanji + katakana), ổn định theo email qua các lần retry."""
    r = get_seeded_random(f"{email}|jpname")
    last_kanji, last_kana = r.choice(_JP_SURNAMES)
    first_kanji, first_kana = r.choice(_JP_GIVEN_NAMES)
    return {
        "last_kanji": last_kanji,
        "first_kanji": first_kanji,
        "last_kana": last_kana,
        "first_kana": first_kana,
    }
