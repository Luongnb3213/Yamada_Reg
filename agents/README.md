# Yamada DOM Agent

Agent chính:

`/Users/macbook/Desktop/FPT/Yamada_Reg/agents/yamada_register_agent.js`

## Lấy data từ Excel

Nếu dùng Crane theo từng nick, chạy bước này trước để tạo/chọn container và ghi ngược vào Excel:

```bash
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/scripts/crane_container_manager.py ensure-row \
  --xlsx /path/to/input.xlsx \
  --row 2
```

Nếu row chưa có `crane_container_id`, lệnh trên mặc định lấy active container hiện tại rồi tịnh tiến sang container kế tiếp. Nếu row đã có container thì chỉ switch lại đúng container đó.

Export dòng đầu tiên có `status` trống/`PENDING`/`FAILED` trong sheet `Inputs`:

```bash
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/scripts/yamada_profile_from_excel.py \
  --xlsx /path/to/input.xlsx
```

Hoặc chỉ định dòng Excel cụ thể:

```bash
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/scripts/yamada_profile_from_excel.py \
  --xlsx /path/to/input.xlsx \
  --row 2
```

Script sẽ sinh:

`/Users/macbook/Desktop/FPT/Yamada_Reg/agents/current_profile.js`

Attach vào app đang mở:

```bash
frida -U -n yamadadenki \
  -l /Users/macbook/Desktop/FPT/Yamada_Reg/agents/yamada_register_agent.js \
  -l /Users/macbook/Desktop/FPT/Yamada_Reg/agents/current_profile.js
```

Trong Frida REPL, data đã được nạp sẵn từ Excel nên chỉ cần:

```js
yamadaScreen()
yamadaStep()
```

Nếu chỉ muốn xem Crane không đụng Excel:

```bash
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/scripts/crane_container_manager.py info
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/scripts/crane_container_manager.py active
```

## Set tay khi test selector

Nếu muốn bỏ qua Excel và set data tay trong Frida REPL:

```js
yamadaDetect()

setYamadaProfile({
  email: "user@example.com",
  pin: "1234",
  phone: "09012345678",
  last_name: "山田",
  first_name: "太郎",
  last_name_kana: "ヤマダ",
  first_name_kana: "タロウ",
  postal_code: "6751234",
  prefecture: "兵庫県",
  city: "加西市",
  address_rest: "北条町1-1",
  dob: "19900101",
  gender: "male"
})

yamadaStep()
```

Khi tới màn nhập mã email:

```js
setYamadaAuthCode("123456")
yamadaStep()
```

Lấy mã tự động từ mail OTP Yamada (`noreply@tpgaw.jp`) cho một dòng Excel:

```bash
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/scripts/fetch_yamada_email_otp.py \
  --xlsx /path/to/input.xlsx \
  --row 2 \
  --write-excel \
  --profile-js /Users/macbook/Desktop/FPT/Yamada_Reg/agents/current_profile.js
```

Sau đó trong Frida REPL có thể chạy lại:

```js
yamadaStep()
```

`yamadaRun()` chạy liên tục qua các màn cho tới khi cần mã email, gặp màn chưa biết, hoặc đã submit form thông tin hội viên.

Nhận diện màn:

```js
yamadaDetect()  // trả state ngắn
yamadaScreen()  // trả state + confidence + matched/missing selectors
```

Các state hiện có:

- `tracking_location_consent`
- `member_register_top`
- `app_home_unregistered`
- `terms_consent`
- `email_register_input`
- `email_register_confirm`
- `unexpected_error_restart`
- `email_auth_code_input`
- `member_info_input`
- `maybe_complete`
