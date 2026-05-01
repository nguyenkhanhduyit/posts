# Facebook Posts Screenshot Tool (Production-ready Skeleton)

Tool tự động tìm kiếm bài viết Facebook theo keyword và chụp ảnh từng bài viết (element screenshot), chạy Chrome **có UI** (không headless), mỗi keyword là **1 job** và chạy **tuần tự** để giảm rủi ro bị block.

## Cấu trúc thư mục

```
.
├─ frontend/
├─ backend/
├─ worker/
├─ utils/
├─ storage/                (tự tạo khi chạy; chứa SQLite)
├─ posts/                  (tự tạo khi chạy)
├─ logs/                   (tự tạo khi chạy)
├─ chrome-profile/         (tự tạo khi chạy; giữ session login)
├─ run.bat
├─ requirements.txt
└─ .env.example
```

## Yêu cầu hệ thống

- Windows 10/11
- Python 3.10+ (khuyến nghị 3.11)
- Chrome/Chromium sẽ được Playwright cài (Chromium) khi chạy lần đầu

## Chạy bằng double-click

1. Copy `.env.example` thành `.env` và đổi `APP_SECRET`.
2. Double-click `run.bat`
3. Mở UI tại: `http://localhost:8080`

## UI có gì

- Nhập Facebook email/phone + password (password **không** lưu plaintext xuống disk; được mã hoá tạm trong DB bằng `APP_SECRET`)
- Nhập keywords (mỗi dòng 1 keyword)
- Cấu hình: `maxPosts (10–20)`, delay hành động, delay giữa keywords
- Start/Stop (stop toàn bộ hoặc stop theo job)
- Danh sách job + progress + trạng thái
- Logs realtime theo job (SSE)

## API nhanh

- `POST /start-job`
- `GET /job-status`
- `GET /logs?jobId=...&offset=...`
- `GET /logs/stream?jobId=...&offset=...` (SSE)
- `POST /stop` (stop toàn bộ hoặc theo jobId)

## Lưu ảnh

Ảnh được lưu theo:

`posts/YYYY-MM-DD/<keyword_sanitized>/post_001.png ...`

## Lưu ý vận hành

- Worker chạy tuần tự (concurrency=1).
- Có anti-block cơ bản: non-headless, typing delay, action delay, random pauses, delay giữa keywords.
- Nếu phát hiện checkpoint/captcha (URL chứa `checkpoint` hoặc page có chữ `captcha`, `Xác minh`, `Verify`) → job `error` và dừng job đó.

## Dev

Chạy thủ công (2 cửa sổ terminal):

```bash
python -m backend.app
```

```bash
python -m worker.runner
```

UI phục vụ từ backend: `http://localhost:8080`

