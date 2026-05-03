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

## Nhận diện "bài viết cần chụp" (AI nhanh)

Tool có thể bật bộ phân loại ảnh nhẹ (CPU-only) để loại các ảnh không phải "bài viết cần chụp" ngay sau khi chụp.

### Dataset (thư mục gốc)

- `post_classifier_data/positive/`: ảnh **là** bài viết cần chụp
- `post_classifier_data/negative/`: ảnh **không** phải bài viết cần chụp

### Train model

Chạy từ thư mục gốc:

```bash
python -m app.worker.post_classifier.train
```

Model sẽ được lưu ở `app/worker/post_classifier/model.json`.

### CNN deep (CPU) — tự động qua `run.bat`

`run.bat` sẽ tự:
- tạo lại `app/.venv` bằng **Python 3.10** nếu venv hiện tại không phải 3.10 (để cài được `numpy/onnxruntime`)
- cài `app/requirements-deep.txt`
- mặc định `POST_CLASSIFIER_ENGINE=deep` (CNN negative-only)

Nếu bạn muốn tắt CNN và dùng hybrid nhanh nhẹ hơn: `set POST_CLASSIFIER_ENGINE=hybrid` trước khi chạy `run.bat`.

### Bật khi chạy worker

Worker đọc `app/.env` qua `load_settings()` ở đầu `app/worker/runner.py`.

- Mặc định: nếu **không** set `POST_CLASSIFIER_ENABLED`, worker sẽ **tự bật** classifier khi tồn tại
  `app/worker/post_classifier/model.json` với `kind` thuộc `{deep_binary_v1, deep_rejector_v1, hybrid_rejector_v1, rejector_v1}`.
  (Tránh bật nhầm với model legacy `linear_v1`.)
- `POST_CLASSIFIER_ENABLED=1` để **ép bật** (kể cả khi model chưa đúng/kind không hỗ trợ).
- `POST_CLASSIFIER_ENABLED=0` để **ép tắt** (kể cả khi đã train xong).
- `POST_CLASSIFIER_BUDGET_SEC=3.5` (giới hạn thời gian nhận diện mỗi ảnh)
- `POST_CLASSIFIER_REJECT_THRESHOLD=0.85` (rejector/negative-only: ngưỡng loại; tăng => loại ít hơn)
- `POST_CLASSIFIER_POS_THRESHOLD=0.60` (binary: giữ ảnh nếu \(P(positive)\) ≥ ngưỡng; tăng => giữ ít hơn)
- `POST_CLASSIFIER_ONNX_VARIANT=fp32|int8` (int8 nhanh hơn, fp32 ổn định hơn)
- `POST_CLASSIFIER_DEEP_RECHECK_MARGIN=0.03` (deep: nếu score sát ngưỡng thì chạy recheck multi-crop; tăng => chắc hơn nhưng chậm hơn)
- `POST_CLASSIFIER_DEEP_EMB_CACHE=256` (deep: cache embedding theo file để retry nhanh hơn; 0 để tắt)
- `POST_CLASSIFIER_ORT_INTER_THREADS=0` (tùy máy: set 1..4 nếu muốn ORT song song hơn)
- `POST_CLASSIFIER_OPENSET_GATING=1` (deep: tăng ngưỡng loại động trên ảnh “khó” để giảm loại nhầm; không cần positive)
- `POST_CLASSIFIER_GATING_MARGIN=0.06` (chỉ gating khi score sát ngưỡng)
- `POST_CLASSIFIER_GATING_MAX_BOOST=0.10` (mức tăng ngưỡng tối đa khi ảnh rất “khó”)

Khi AI từ chối, ảnh sẽ được chuyển vào folder con: `<run_folder>/_rejected/`.

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

