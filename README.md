# Yamada_Reg Wrapper

Khung này được tách từ phần wrapper của `Neppi_Pay`, đã bỏ app shell/CLI/worker flow chính và chỉ giữ lại UI tối giản riêng để chạy thử Yamada.

Đã giữ lại:

- cấu hình runtime trong `src/config.py`
- logging có prefix theo worker
- kết nối XLSX an toàn: lock, backup, atomic save, template, account/proxy sheets
- proxy pool, proxy health-check, circuit breaker
- Playwright browser helper
- helper OTP/SMS/email mức hạ tầng
- một số tiện ích dữ liệu chung
- Frida DOM agent cho flow đăng ký email: `agents/yamada_register_agent.js`
- Excel-to-agent profile exporter: `scripts/yamada_profile_from_excel.py`
- Crane container manager theo từng account: `scripts/crane_container_manager.py`
- Yamada email OTP fetcher qua IMAP: `scripts/fetch_yamada_email_otp.py`
- UI tối giản để chạy thử: `gui.py`

Không copy:

- `main.py`, `api_main.py`
- UI/worker cồng kềnh của Neppi; Yamada chỉ có `gui.py` tối giản riêng
- `src/worker.py`, `src/api_worker.py`
- `src/flows/*`
- các cột/trạng thái đặc thù Neppi như `bnid_user_code`, `two_step_status`, `bandai_password`, `namco_password`, `pbandai_*`

Flow Yamada có thể đặt sau vào `src/flows/` hoặc module riêng, rồi dùng lại các wrapper này.

## UI chạy thử

Mở UI desktop tối giản kiểu `Neppi_Pay/gui.py`:

```bash
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/gui.py
```

Trong UI, nút `Chạy row` sẽ tự gọi `ensure-row`, xuất profile, chạy DOM, tự lấy OTP email khi app tới màn nhập mã, rồi chạy tiếp. Nếu row chưa có `crane_container_id`, tool tự tạo container mới rồi chạy. Không cần gọi `next` thủ công trong luồng chạy.

Ô `Wait màn (s)` điều khiển thời gian chờ mỗi lần app chuyển trang. Mặc định 15 giây; agent sẽ poll DOM tới khi state/url đổi thay vì sleep cứng.

Thứ tự test nhanh: chọn Excel, sheet, row rồi bấm `Chạy row`. Nếu không lấy được OTP email, tool ghi `FAILED` và lỗi vào `error_details` của dòng Excel.

## Cột đầu vào Yamada

Workbook đầu vào dùng 3 sheet provider như Neppi: `Outlooks`, `Gmails`, `Iclouds`. Mỗi sheet dùng các cột:

`email`, `pin`, `phone`, `last_name`, `first_name`, `last_name_kana`, `first_name_kana`, `postal_code`, `prefecture`, `city`, `address_rest`, `dob`, `gender`, `password`, `otp_inbox`, `otp_password`, `otp_imap_host`, `crane_container_id`, `crane_container_name`, `crane_status`, `crane_assigned_at`, `crane_last_used_at`, `status`, `error_details`, `notes`.

`email` dùng luôn cho ô email nhập lại trên form. `dob` dùng dạng `YYYYMMDD`.

## Crane theo từng nick

Script này dùng đúng hướng từ `/Users/macbook/crane_stress.py`: spawn host `com.opa334.CraneApplication`, attach Frida vào host đang bị freeze, rồi gọi `CraneManager` để thao tác container cho bundle Yamada `jp.co.unisys.yamadamobile`.

Kiểm tra Crane/Yamada hiện tại:

```bash
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/scripts/crane_container_manager.py info
```

Các thao tác Crane đang có:

```bash
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/scripts/crane_container_manager.py list
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/scripts/crane_container_manager.py active
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/scripts/crane_container_manager.py create --name Yamada_test
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/scripts/crane_container_manager.py next  # chỉ debug/test tay
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/scripts/crane_container_manager.py switch --container-id <container_id>
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/scripts/crane_container_manager.py delete --container-id <container_id>
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/scripts/crane_container_manager.py delete-content --container-id <container_id>
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/scripts/crane_container_manager.py wipe --container-id <container_id>
```

Tạo hoặc chọn container cho một dòng Excel, reload app Yamada, và ghi ngược `crane_container_*` vào sheet:

```bash
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/scripts/crane_container_manager.py ensure-row \
  --xlsx /path/to/input.xlsx \
  --row 2
```

Nếu dòng đó đã có `crane_container_id`, script chỉ switch lại container đó. Nếu chưa có, mặc định script tạo container mới, reload Yamada, rồi ghi lại Excel.

Nếu muốn tạo mới thay vì dùng container có sẵn:

```bash
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/scripts/crane_container_manager.py ensure-row \
  --xlsx /path/to/input.xlsx \
  --row 2 \
  --container-mode create
```

Nếu gặp `device not found`, kiểm tra lại iPhone đang cắm USB, Frida server trên máy đã chạy, hoặc truyền thẳng device id bằng `--device-id <id>`.

Sau đó export data dòng đó cho DOM agent:

```bash
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/scripts/yamada_profile_from_excel.py \
  --xlsx /path/to/input.xlsx \
  --row 2
```

## Lấy OTP email Yamada

Mail OTP Yamada lọc theo sender `noreply@tpgaw.jp`; mail hoàn tất có thể đến từ `noreply@ml.yamada-denki.jp`. Script ưu tiên mã cạnh từ khóa `認証コード` và tránh nhầm `会員番号` trong mail hoàn tất đăng ký.

IMAP tự nhận domain phổ biến như Gmail, Outlook/Hotmail/Live, iCloud/Me/Mac, Yahoo, AOL, Zoho, GMX, Yandex, Mail.ru. Với domain lạ, điền `otp_imap_host` trong Excel hoặc truyền `--imap-host`. Excel nhận cả tên cột kiểu Neppi `otp_email`/`otp_pass`; tool tự map sang `otp_inbox`/`otp_password`.

Lấy OTP cho dòng Excel và tạo lại `current_profile.js`. OTP không được ghi lại vào Excel:

```bash
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/scripts/fetch_yamada_email_otp.py \
  --xlsx /path/to/input.xlsx \
  --row 2 \
  --profile-js /Users/macbook/Desktop/FPT/Yamada_Reg/agents/current_profile.js
```

Nếu dùng mailbox riêng, truyền thẳng:

```bash
python3 /Users/macbook/Desktop/FPT/Yamada_Reg/scripts/fetch_yamada_email_otp.py \
  --email user@example.com \
  --inbox user@example.com \
  --password '<imap_or_app_password>'
```
