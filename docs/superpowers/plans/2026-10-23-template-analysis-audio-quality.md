# Kế hoạch triển khai template MAIN và audio quality

Ngày: 2026-10-23.
Spec: ../specs/2026-10-22-template-analysis-audio-quality-design.md.

Mục tiêu: một worker Rhythm xử lý tuần tự, phân tích template thành JSON trên volume, DB lưu metadata/key; quality cho template và student. Giữ evaluate_pair và backend/frontend như hiện tại.

## Task 1 — Quality và cấu hình

Files: app/config.py, app/constants/audio_quality.py, app/services/audio_quality_service.py, tests/test_audio_quality.py.
Interface: policy(), measure(samples, sample_rate, policy=None), async inspect(path, policy=None), ensure_acceptable(report).
- [x] Viết test PASS/WARN/REJECT, ngưỡng và stereo; chạy để thấy chức năng chưa có.
- [x] Triển khai phép đo ở sample rate/kênh gốc, snapshot chính sách.
- [x] Chạy lại test; phân biệt lỗi nội dung với timeout/thiếu decoder.

## Task 2 — Contract, metadata, JSON và pipeline template

Files: app/schemas/template_messages.py, app/models/template_analysis.py, app/repositories/template_analysis_repository.py, app/services/artifact_storage_service.py, app/services/template_feature_service.py, app/services/template_analysis_service.py, app/services/job_execution_service.py, tests/test_template_analysis.py.
Interface: TemplateAnalysisTask; repository get/create_if_missing/lookup; storage write/read; service process_task/record_failure.
- [x] Test trước khi triển khai contract, MAIN/audio ID, duplicate/conflict, JSON/path/checksum.
- [x] Tạo pipeline tải → quality → F0/tempo → file nguyên tử → commit metadata.
- [x] Đối chiếu F0 với helper cũ và chạy pipeline với audio thật.

## Task 3 — Quality student và một job tại một thời điểm

Files: app/models/evaluation.py, app/repositories/evaluation_repository.py, app/services/evaluation_service.py, tests/test_evaluation_quality.py, tests/test_current_worker_behavior.py.
- [x] Test trước khi thêm quality và khóa; xác nhận lỗi chất lượng chưa bị chặn.
- [x] Lưu báo cáo nhỏ, giữ result null khi REJECT, PASS/WARN tiếp tục chấm.
- [x] Dùng chung khóa giữa template, queue so sánh và HTTP; kiểm tra cancellation.
- [x] Chạy lại các test orchestration cũ với dependency quality giả.

## Task 4 — Một worker nhận hai queue

Files: app/messaging/rabbitmq_worker.py, tests/test_template_worker.py.
- [x] Test handler còn thiếu, payload/conflict, ACK sau persistence, retry/confirm và DB lỗi.
- [x] Nối consumer template vào worker hiện tại, không đọc/xóa DLQ mới.
- [x] Test timeout kỹ thuật có retry và phục hồi khi file đã ghi nhưng commit thất bại.

## Task 5 — API tra cứu/đọc artifact

Files: app/routes/template_analysis_routes.py, app/api.py, tests/test_template_api.py.
- [x] Test endpoint chưa có, auth, lookup chính xác và trường hợp lỗi.
- [x] Lookup trước route UUID; không fallback về completed cũ; kiểm tra checksum/path.
- [x] Xác nhận API evaluation vẫn trả mã lỗi quality và result null.

## Task 6 — Schema, vòng đời, triển khai và rà soát

Files: migrations/env.py, migrations/versions/20261023_0002_template_analysis_quality.py, app/main.py, docker-compose.yml, .env.example, .gitignore, README.md, tests/test_migration_and_runtime.py.
- [x] Test migration và worker chết thì API dừng trước khi nối chức năng.
- [x] Thêm migration Rhythm, volume JSON, cấu hình và hướng dẫn sử dụng.
- [x] Kiểm tra upgrade/downgrade trên SQLite tạm; Compose dùng .env.example hợp lệ.
- [x] Rà soát độc lập, viết test tái hiện các lỗi phát hiện.
- [x] Chạy lại toàn bộ test và kiểm tra diff sau sửa cuối: 84 passed, syntax 46 file Python hợp lệ, git diff --check và Compose config đều qua.

## Ràng buộc và giới hạn

Không sửa .env thật, backend/frontend hoặc thuật toán chấm; không commit/push hay chạy migration trên DB đang dùng. Giữ thay đổi có sẵn trong checkout. Chỉ một worker/process, không thêm lease, heartbeat hoặc cơ chế phân tán.

Docker engine chưa chạy, nên chưa kiểm chứng PostgreSQL/RabbitMQ thật hoặc restart volume container. Chạy test không kết nối các dịch vụ sản phẩm.

## Điều chỉnh sau triển khai

Theo yêu cầu người dùng, năm ngưỡng quality dùng constant trong source, không lấy từ env. Giữ snapshot cho job template đang retry.
