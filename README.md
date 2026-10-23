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

Audio URL phải là HTTP/HTTPS. Service cho phép localhost và địa chỉ mạng nội bộ mà worker truy cập được. Nếu cấu hình `RHYTHM_SERVICE_ALLOWED_HOSTS`, hostname của storage phải nằm trong danh sách này. Khi worker chạy trong Docker, localhost trỏ đến chính container worker.

## Phân tích trước bản MAIN

Hướng dẫn tích hợp cho BE: [gửi job MAIN, theo dõi trạng thái và lấy JSON](docs/guides/backend-template-analysis.md).

Chạy `uv run python -m app.main` hoặc Docker Compose như hiện tại. Chỉ chạy một instance/process Rhythm. Một worker nhận hai queue, dùng chung khóa với HTTP background task để xử lý tối đa một job tại mỗi thời điểm. API đọc trạng thái vẫn phục vụ trong lúc phân tích.

Queue template durable: `rhythm.template.analyze.v1`. DLQ: `rhythm.template.analyze.dlq`; service không tự đọc/xóa message trong DLQ này. Chưa nối publisher backend. Có thể gửi payload mẫu bằng công cụ quản trị RabbitMQ:

```json
{
  "messageId": "0acaf37c-4ee8-42c5-9dfd-ed87a5b3d0ab",
  "analysisId": "5820ae1c-5102-4df9-91fb-dfa02bc734ee",
  "sheetMusicId": "d067917d-eb88-473c-8dab-771965f20575",
  "mediaFileId": "fbda6f08-83af-42ac-a2ec-587a99af0e40",
  "mediaUrl": "https://storage.example/template.wav",
  "instrumentCode": "dan_bau",
  "requestedAt": "2026-10-23T00:00:00Z"
}
```

Các ID phải là UUID, timestamp có múi giờ. Thay URL bằng file worker tải được. `sheetMusicId` là ID MAIN; `mediaFileId` là `MAIN.referenceAudioId`, không phải ID PDF/ảnh bản nhạc. Caller chịu trách nhiệm xác minh quan hệ MAIN/audio. Rhythm không truy vấn database backend.

Nhạc cụ nhận các code: `dan_bau`, `dan_nhi`, `dan_nguyet`, `dan_tranh`, `dan_ty_ba`, `sao_truc`. Không truyền UUID Instrument hoặc code viết hoa của backend vào trường này.

Worker kiểm tra quality, trích F0/tempo bằng phương pháp hiện có rồi ghi JSON. File chứa MAIN/audio ID, hash nguồn, cấu hình nhạc cụ, quality, `f0Hz` (`null` khi không có cao độ), `voicedFlags`, sample rate/hop length, tempo và phiên bản. Template không gọi Gemini. Database giữ metadata cùng key:

```text
template-analyses/{sheetMusicId}/{mediaFileId}/{analysisId}/result.json
```

`RHYTHM_SERVICE_ARTIFACT_ROOT` độc lập thư mục tạm; Docker Compose mount named volume `rhythm-artifacts` vào đây. Giữ volume cùng bản sao DB khi sao lưu/khôi phục; tránh `docker compose down -v` nếu cần giữ kết quả. Chạy Python local trên Windows có thể đặt root thành `./artifacts` trong cấu hình cá nhân.

Job completed cùng ID/đầu vào được bỏ qua; cùng ID nhưng khác MAIN/audio/URL/nhạc cụ vào DLQ. Sau sự cố, message chưa ACK được broker giao lại và bản ghi processing được chạy lại. Retry tạm thời theo `x-retry-count` và giới hạn hiện có, không có retry queue riêng. Muốn chạy lại job failed cuối cùng hoặc đổi cấu hình thì tạo analysisId mới. URL phải có nội dung ổn định; thay audio cần mediaFileId và job mới.

## API template nội bộ

Dùng `Authorization: Bearer <RHYTHM_SERVICE_INTERNAL_API_KEY>`:

```text
GET /api/v1/template-analyses/lookup?sheetMusicId=<UUID>&mediaFileId=<UUID>&instrumentCode=dan_bau&analysisVersion=template-pyin-v1
GET /api/v1/template-analyses/<analysisId>
GET /api/v1/template-analyses/<analysisId>/artifact
```

Lookup yêu cầu đủ bốn tham số và trả lần phân tích mới nhất, gồm analysisId, MAIN/audio ID, trạng thái, quality và artifactUrl. Không có bản khớp: 404; bản mới nhất chưa completed: 409, không fallback về completed cũ. Artifact trả JSON và kiểm tra checksum; file thiếu/hỏng trả 503. Status cho đọc lỗi của job failed.

Khi nối backend sau này: từ pieceId và nhạc cụ trong ngữ cảnh lớp/bài tập, dùng `findMain(pieceId, instrumentId)` để lấy `main.id`/`main.referenceAudioId`, rồi lookup Rhythm. Một piece có MAIN riêng cho từng nhạc cụ. Không dùng kết quả của mediaFileId cũ sau khi thay audio.

## Quality template và student

Đo trên sample rate/kênh nguồn trước resample, mono hoặc normalization. WAV/MP3 dùng soundfile; M4A có fallback FFmpeg đã được cài trong Docker image. Cần FFmpeg trong PATH nếu chạy local và dùng định dạng cần fallback.

- REJECT: nội dung không giải mã được, không có mẫu, NaN/Infinity hoặc toàn bộ mẫu bằng 0. Lưu lỗi vĩnh viễn, không chấm/Gemini và không retry nội dung đó.
- WARN: RMS dưới -45 dBFS, hơn 80% thời lượng nằm trong khung RMS dưới -60 dBFS, hoặc hơn 1% mẫu có biên độ tuyệt đối từ 0.999. Vẫn phân tích/chấm.
- PASS: không có lỗi hoặc cảnh báo trên.

Thiếu FFmpeg hoặc timeout giải mã là lỗi kỹ thuật (`AUDIO_DECODER_UNAVAILABLE`, `AUDIO_DECODE_TIMEOUT`), không kết luận bản thu hỏng. Queue retry các lỗi này có giới hạn; HTTP background task lưu lỗi cụ thể theo hành vi hiện tại.

Các ngưỡng quality nằm trong `app/constants/audio_quality.py`, không đọc từ env. Đây là điểm khởi đầu, cần hiệu chỉnh bằng dữ liệu thật. Khi đổi hành vi/ngưỡng, tăng `POLICY_VERSION`; job template đang retry vẫn dùng snapshot đã lưu. Không chặn sai nốt/nhịp, không tự khử nhiễu hoặc sửa waveform. Bản thu gần im lặng nhưng không bằng 0 chỉ cảnh báo.

Báo cáo student lưu ở `student_quality_report`; khi completed có thêm `result.studentAudioQuality`. Khi quality từ chối: `failed`, `result: null`, mã lỗi và thông báo tiếng Việt. Backend hiện lọc trường result bổ sung nên frontend chưa tự hiện cảnh báo quality.

Luồng so sánh vẫn nhận hai URL và phân tích lại template bằng `evaluate_pair`. Chưa dùng JSON template để chấm; phần đó dành cho giai đoạn sau.

## Schema và kiểm thử

Migration Rhythm `20261023_0002` tạo `template_analyses` và thêm `student_quality_report` vào `rhythm_evaluations`; service/CI áp dụng theo cơ chế Alembic hiện có. Không chạy migration lên database đang dùng chỉ để kiểm thử.

```powershell
uv run pytest -q
```

Test dùng SQLite tạm, audio tổng hợp và broker giả; không kết nối broker/database sản phẩm. Có kiểm tra quality, JSON/checksum, liên kết MAIN/audio, ACK/retry, API, phục hồi sau lỗi commit, migration và xử lý tuần tự. Kiểm chứng PostgreSQL/RabbitMQ thật và volume qua restart container cần môi trường tích hợp riêng.
