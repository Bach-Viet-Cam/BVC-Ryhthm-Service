# Rhythm Service

All environment variables consumed by this service use the `RHYTHM_SERVICE_` prefix. Update local `.env` files to match `.env.example` before starting the service.

Worker phân tích audio cho Bach Viet Cam. Service đọc task từ RabbitMQ, tải hai audio URL, chạy pYIN/DTW, lưu kết quả vào PostgreSQL và cung cấp API nội bộ để backend đọc kết quả.

## Chạy local

```powershell
cd rhythm-service
uv sync
Copy-Item .env.example .env
uv run python -m app.main
```

Local cần RabbitMQ Cloud và PostgreSQL. Cấu hình `RABBITMQ_URL` trong file `.env` ở root trước khi chạy:

```powershell
docker compose up --build rhythm-db rhythm-service
```

`RABBITMQ_URL` phải là AMQPS URL do nhà cung cấp RabbitMQ Cloud cấp, ví dụ `amqps://user:password@host.cloudamqp.com/vhost`. File audio được giới hạn tối đa 25 MB (`MAX_FILE_BYTES=26214400`).

Dependency workflow:

```powershell
uv add <package>
uv lock
uv sync
uv run python -m app.main
```

Commit cả `pyproject.toml` và `uv.lock`. Docker dùng `uv sync --locked` để không tự thay đổi dependency trong lúc build.

Sau khi phân tích audio, service gọi Gemini để tạo `aiFeedback` và lưu cùng `result_json`. Nếu chưa cấu hình `GEMINI_API_KEY` hoặc Gemini lỗi, kết quả phân tích vẫn được lưu với nội dung fallback.

## Database migration

Schema được quản lý bằng Alembic. Service tự chạy `alembic upgrade head` trước khi mở HTTP server và RabbitMQ worker.

Khi thay đổi model:

```powershell
uv run alembic revision --autogenerate -m "describe schema change"
uv run alembic upgrade head
```

Luôn kiểm tra migration được sinh ra trước khi commit. Với database hiện tại đã được tạo bằng `create_all()` trước khi Alembic được thêm vào, chạy một lần:

```powershell
uv run alembic stamp 20260926_0001
```

Lệnh `stamp` chỉ đánh dấu migration baseline đã được áp dụng, không xóa hoặc thay đổi dữ liệu. Database mới sẽ tự tạo schema bằng `alembic upgrade head`.

## RabbitMQ task

Queue mặc định: `rhythm.evaluate.v1`.

```json
{
  "messageId": "msg_123",
  "evaluationId": "evaluation_123",
  "templateUrl": "https://storage.example.com/template.wav",
  "studentUrl": "https://storage.example.com/student.wav",
  "instrumentId": "dan_bau",
  "requestedAt": "2026-09-25T10:00:00Z"
}
```

Task được ACK sau khi kết quả đã commit vào database. Lỗi tạm thời được retry tối đa `MAX_RETRIES` lần; lỗi không thể retry được lưu với trạng thái `failed` và ACK. Task sai JSON được chuyển sang `rhythm.evaluate.dlq`.

## API nội bộ

```text
GET /health
POST /api/v1/evaluations
GET /api/v1/evaluations/{evaluationId}
Authorization: Bearer <INTERNAL_API_KEY>
```

Backend có thể gửi job trực tiếp qua HTTP:

```json
{
  "messageId": "msg_123",
  "evaluationId": "evaluation_123",
  "templateUrl": "https://storage.example.com/template.wav",
  "studentUrl": "https://storage.example.com/student.wav",
  "instrumentId": "dan_bau"
}
```

Endpoint trả `202 Accepted` với trạng thái `queued`. Service xử lý background và lưu kết quả vào database. Backend dùng `GET /api/v1/evaluations/{evaluationId}` để polling kết quả. Queue worker và HTTP API dùng chung một pipeline xử lý.

Kết quả có các trạng thái `queued`, `processing`, `completed`, `failed`. `evaluationId` là unique key để xử lý message bị giao lại mà không tạo bản ghi trùng.

## Lưu ý bảo mật

Audio URL phải là HTTP/HTTPS. Service chặn loopback, private IP, link-local IP và reserved IP để hạn chế SSRF. Nên cấu hình `ALLOWED_HOSTS` với domain của object storage khi triển khai production.
