# Rhythm Service

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
